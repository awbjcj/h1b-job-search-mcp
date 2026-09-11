#!/usr/bin/env python3
import asyncio
import csv
import os
import re
import subprocess
import sys
import threading
import zipfile
from collections.abc import Sequence
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime
from functools import wraps
from html import unescape
from typing import Dict, Optional, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from disclosure_store import DisclosureStore
from employers import _normalise_employer as _normalise_employer
from file_cache import query_file_cache

DATA_CACHE_DIR = os.environ.get(
    "H1B_DATA_CACHE_DIR", os.path.join(os.path.dirname(__file__), "..", "data_cache")
)
os.makedirs(DATA_CACHE_DIR, exist_ok=True)
# SQLite sorting uses the volume too, including inside the importer subprocess.
os.environ.setdefault("SQLITE_TMPDIR", os.path.join(DATA_CACHE_DIR, "tmp"))
os.makedirs(os.environ["SQLITE_TMPDIR"], exist_ok=True)
DOL_PERFORMANCE_URL = "https://www.dol.gov/agencies/eta/foreign-labor/performance"
DOL_REQUEST_HEADERS = {
    "User-Agent": "h1b-job-search-mcp/1.0 (+https://www.dol.gov/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
LCA_DISCLOSURE_PATTERN = re.compile(
    r"LCA_Dis(?:l)?closure_Data_FY(20\d{2})_Q([1-4])\.xlsx",
    re.IGNORECASE,
)
LCA_DISCLOSURE_LINK_PATTERN = re.compile(
    r'''href\s*=\s*["']([^"']*LCA_Dis(?:l)?closure_Data_FY20\d{2}_Q[1-4]\.xlsx[^"']*)["']''',
    re.IGNORECASE,
)
CACHE_ARTIFACT_PATTERN = re.compile(
    r"LCA_(\d{4})Q([1-4])\.(pkl|xlsx|sqlite)",
    re.IGNORECASE,
)
LATEST_DISCOVERY_LOOKBACK_YEARS = 5
RECENT_SPONSORSHIP_QUARTERS = 6
HISTORICAL_CACHE_YEARS = RECENT_SPONSORSHIP_QUARTERS / 4

class H1BDataManager:
    def __init__(self):
        self.store: DisclosureStore | None = None
        self.last_loaded = None
        self.current_file = None
        self.loaded_year = None
        self.loaded_quarter = None
        self.source_url = None
        self.latest_available_year = None
        self.latest_available_quarter = None
        self.latest_checked = None
        self.discovered_periods: list[tuple[int, int]] = []
        self.discovered_urls: dict[tuple[int, int], list[str]] = {}
        # Serialize period selection, import, and tool queries across workers.
        self._data_lock = threading.RLock()
        # The background startup warmup (asyncio.to_thread) and any MCP tool
        # call run on separate threads and share this instance. Without a
        # per-period lock, two threads downloading the same quarter both
        # write to the identical temp .xlsx path and corrupt each other's
        # download (observed as truncated files / "not a zip file" errors).
        self._period_locks: dict[tuple[int, int], threading.Lock] = {}
        self._period_locks_guard = threading.Lock()

    def _lock_for_period(self, year: int, quarter: int) -> threading.Lock:
        key = (year, quarter)
        with self._period_locks_guard:
            lock = self._period_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._period_locks[key] = lock
            return lock

    @contextmanager
    def operation(self):
        """Serialize access to the selected disk-backed disclosure."""
        with self._data_lock:
            yield

    def _activate_store(self, path, year, quarter, source_url) -> DisclosureStore:
        store = DisclosureStore(path)
        self.store = store
        self.current_file = path
        self.last_loaded = datetime.now()
        self.loaded_year = year
        self.loaded_quarter = quarter
        self.source_url = source_url
        return store

    def _convert_cache(self, source, destination):
        # Keep import-only libraries and transient allocations out of the server.
        subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), 'import_disclosure.py'),
             source, destination], check=True, timeout=1800,
        )

    def get_dol_urls(self, year: int, quarter: int) -> list:
        """Generate DOL URLs based on actual file naming patterns from the DOL website"""
        # Prefer the exact href published by DOL. The newest disclosures have
        # moved between the legacy /oflc/pdfs path and /media, and some file
        # names contain DOL's long-standing "Dislclosure" typo.
        urls = list(self.discovered_urls.get((year, quarter), []))
        
        # Base URL for DOL OFLC PDFs directory
        base_dol = "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs"
        
        # Based on the actual DOL page, current and recent fiscal years use
        # LCA_Disclosure_Data_FY{year}_Q{quarter}.xlsx.
        # Older years use several historical naming patterns.
        # For older years (pre-2020): H-1B FY2019.xlsx or H1B FY2017.xlsx
        
        if year >= 2020:
            # Modern naming convention (2020+)
            urls.append(f"{base_dol}/LCA_Disclosure_Data_FY{year}_Q{quarter}.xlsx")
            # DOL currently has a published FY2026 file with this spelling
            # typo; keep both variants so discovery and download agree.
            urls.append(f"{base_dol}/LCA_Dislclosure_Data_FY{year}_Q{quarter}.xlsx")

            # DOL has started publishing its newest LCA file under
            # dol.gov/media/ instead of the legacy oflc/pdfs directory
            # (older quarters still live at the legacy path). Offer both
            # hosts so the newest period is downloadable as soon as it is
            # discovered.
            media_base = "https://www.dol.gov/media"
            urls.append(f"{media_base}/LCA_Disclosure_Data_FY{year}_Q{quarter}.xlsx")
            urls.append(f"{media_base}/LCA_Dislclosure_Data_FY{year}_Q{quarter}.xlsx")

            # Some years use different patterns for different quarters
            if year == 2020:
                # 2020 uses a different pattern
                urls.append(f"{base_dol}/LCA_FY{year}_Q{quarter}.xlsx")
            
        else:
            # Older naming conventions (pre-2020)
            if quarter == 4 or quarter == 1:  # Often only annual files for older years
                urls.extend([
                    f"{base_dol}/H-1B_FY{year}.xlsx",
                    f"{base_dol}/H-1B FY{year}.xlsx",  # With space
                    f"{base_dol}/H1B_FY{year}.xlsx",
                    f"{base_dol}/H1B FY{year}.xlsx",   # With space
                    f"{base_dol}/LCA_FY{year}.xlsx",
                    f"{base_dol}/LCA FY{year}.xlsx",   # With space
                ])
        
        # Fallback: Try the flcdatacenter.com when it's back online
        # (currently down due to funding lapse)
        urls.append(f"https://www.flcdatacenter.com/download/LCA_{year}Q{quarter}.xlsx")
        
        # Preserve priority while avoiding duplicate attempts when the exact
        # discovered URL is also one of the generated compatibility URLs.
        return list(dict.fromkeys(urls))
    
    def get_cached_periods(self) -> list[tuple[int, int]]:
        """Return cached fiscal periods ordered from oldest to newest."""
        if not os.path.exists(DATA_CACHE_DIR):
            return []

        periods = set()
        for file_name in os.listdir(DATA_CACHE_DIR):
            match = re.fullmatch(r"LCA_(\d{4})Q([1-4])\.(?:pkl|sqlite)", file_name)
            if match:
                periods.add((int(match.group(1)), int(match.group(2))))
        return sorted(periods)

    def newest_cached_period(self) -> tuple[int, int] | None:
        """Return the newest cached fiscal period, if one exists."""
        periods = self.get_cached_periods()
        return periods[-1] if periods else None

    def prune_cache(
        self,
        keep_periods: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        """Delete cached artifacts outside the current six-quarter window."""
        # Queries now read files throughout an operation. Hold the same data
        # lock they use, then the per-period lock (the load_data lock order).
        with self._data_lock:
            return self._prune_cache(keep_periods)

    def _prune_cache(self, keep_periods: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not os.path.exists(DATA_CACHE_DIR):
            return []

        keep = set(keep_periods)
        stale_periods: set[tuple[int, int]] = set()
        for file_name in os.listdir(DATA_CACHE_DIR):
            match = CACHE_ARTIFACT_PATTERN.fullmatch(file_name)
            if match is not None:
                period = (int(match.group(1)), int(match.group(2)))
                if period not in keep:
                    stale_periods.add(period)

        removed: set[tuple[int, int]] = set()
        for period in stale_periods:
            # Coordinate with load_data so an active read/download for the
            # same period finishes before its stale artifact is removed.
            with self._lock_for_period(*period):
                for extension in ("pkl", "xlsx", "sqlite"):
                    artifact_path = os.path.join(
                        DATA_CACHE_DIR,
                        f"LCA_{period[0]}Q{period[1]}.{extension}",
                    )
                    try:
                        os.remove(artifact_path)
                        removed.add(period)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        print(
                            "Could not remove stale cache artifact "
                            f"{artifact_path}: {error}"
                        )

        for year, quarter in sorted(removed):
            print(f"Removed stale LCA cache for FY{year} Q{quarter}")
        return sorted(removed)

    def latest_known_period(self) -> tuple[int, int] | None:
        """Return the published latest period, then cache/current fallbacks."""
        if (
            self.latest_available_year is not None
            and self.latest_available_quarter is not None
        ):
            return self.latest_available_year, self.latest_available_quarter
        return self.newest_cached_period() or (
            (self.loaded_year, self.loaded_quarter)
            if self.loaded_year is not None and self.loaded_quarter is not None
            else None
        )

    @staticmethod
    def periods_ending_at(
        year: int,
        quarter: int,
        count: int = RECENT_SPONSORSHIP_QUARTERS,
    ) -> list[tuple[int, int]]:
        """Return ``count`` fiscal quarters, newest first, from an anchor."""
        periods: list[tuple[int, int]] = []
        for _ in range(max(0, count)):
            periods.append((year, quarter))
            quarter -= 1
            if quarter == 0:
                year -= 1
                quarter = 4
        return periods

    def _record_latest_period(self, period: tuple[int, int]) -> tuple[int, int]:
        self.latest_available_year, self.latest_available_quarter = period
        self.latest_checked = datetime.now()
        return period

    def discover_latest_period(self) -> tuple[int, int] | None:
        """Discover the newest LCA disclosure period published by DOL.

        The DOL performance page is the source of truth. If that page is
        temporarily unavailable, probe the recent direct-file URLs before
        falling back to the newest local cache.
        """
        try:
            response = requests.get(
                DOL_PERFORMANCE_URL,
                headers=DOL_REQUEST_HEADERS,
                timeout=30,
            )
            response.raise_for_status()
            discovered_urls: dict[tuple[int, int], list[str]] = {}
            for href in LCA_DISCLOSURE_LINK_PATTERN.findall(response.text):
                match = LCA_DISCLOSURE_PATTERN.search(href)
                if match is None:
                    continue
                period = (int(match.group(1)), int(match.group(2)))
                discovered_url = urljoin(DOL_PERFORMANCE_URL, unescape(href))
                parsed_url = urlsplit(discovered_url)
                discovered_url = urlunsplit(
                    (
                        parsed_url.scheme,
                        parsed_url.netloc,
                        re.sub(r"/{2,}", "/", parsed_url.path),
                        parsed_url.query,
                        parsed_url.fragment,
                    )
                )
                discovered_urls.setdefault(period, []).append(discovered_url)

            periods = {
                (int(year), int(quarter))
                for year, quarter in LCA_DISCLOSURE_PATTERN.findall(response.text)
            }
            if periods:
                self.discovered_periods = sorted(periods, reverse=True)
                self.discovered_urls = {
                    period: list(dict.fromkeys(urls))
                    for period, urls in discovered_urls.items()
                }
                return self._record_latest_period(max(periods))
            print("DOL performance page did not list any LCA disclosure files")
        except requests.exceptions.RequestException as error:
            print(f"Could not read the DOL performance page: {error}")
        except Exception as error:
            print(f"Could not parse the DOL performance page: {error}")

        self.discovered_periods = []
        self.discovered_urls = {}
        return self._probe_latest_periods()

    def _probe_latest_periods(self) -> tuple[int, int] | None:
        """Find the newest recent period when the DOL page cannot be read."""
        now = datetime.now()
        current_fiscal_year = now.year + (1 if now.month >= 10 else 0)

        for year in range(
            current_fiscal_year,
            current_fiscal_year - LATEST_DISCOVERY_LOOKBACK_YEARS - 1,
            -1,
        ):
            for quarter in range(4, 0, -1):
                for url in self.get_dol_urls(year, quarter):
                    if "dol.gov" not in url:
                        continue
                    try:
                        response = requests.get(
                            url,
                            headers=DOL_REQUEST_HEADERS,
                            stream=True,
                            timeout=15,
                        )
                        try:
                            content_type = response.headers.get("content-type", "").lower()
                            if response.status_code == 200 and "text/html" not in content_type:
                                return self._record_latest_period((year, quarter))
                        finally:
                            response.close()
                    except requests.exceptions.RequestException:
                        continue

        return None

    def load_latest_data(self, force_download: bool = False) -> bool:
        """Discover, download, and cache the newest available LCA period."""
        latest_period = self.discover_latest_period()
        if latest_period is None:
            latest_period = self.newest_cached_period()
            if latest_period is None:
                print("No current DOL period or cached LCA data is available")
                return False
            print(
                "Using the newest cached LCA period because the current DOL "
                f"period could not be discovered: FY{latest_period[0]} Q{latest_period[1]}"
            )

        # The DOL performance page can list a file before the corresponding
        # download URL is usable. Try the newest cached period first, then
        # fall back through other periods DOL actually publishes so startup
        # can use the newest valid data.
        candidate_periods = [latest_period]
        cached_period = self.newest_cached_period()
        if cached_period and cached_period not in candidate_periods:
            candidate_periods.append(cached_period)

        if self.discovered_periods:
            # Only fall back to periods DOL has confirmed it publishes.
            # Guessing prior quarters by arithmetic can land on stale,
            # unlisted files that still resolve on the legacy host but are
            # no longer the current data (e.g. a superseded single-quarter
            # file after DOL replaces it with a combined-quarter release).
            for period in self.discovered_periods:
                if period not in candidate_periods:
                    candidate_periods.append(period)
        else:
            # No confirmed period listing is available (the DOL page itself
            # could not be read); guess backward from the probed period.
            year, quarter = latest_period
            for _ in range(4):
                quarter -= 1
                if quarter == 0:
                    year -= 1
                    quarter = 4
                previous_period = (year, quarter)
                if previous_period not in candidate_periods:
                    candidate_periods.append(previous_period)

        for index, period in enumerate(candidate_periods):
            if index > 0:
                print(
                    "Falling back to LCA period: "
                    f"FY{period[0]} Q{period[1]}"
                )
            if self.load_data(
                *period,
                force_download=force_download if index == 0 else False,
            ):
                self.prune_cache(self.periods_ending_at(*period))
                return True

        return False

    def cache_recent_data(self, force_download: bool = False) -> list[tuple[int, int]]:
        """Cache the latest six fiscal quarters and leave the newest period loaded.

        Individual tool calls should remain cheap and query one quarter by
        default.  Startup performs this best-effort warmup once so every
        selectable quarter can subsequently be read from the local cache.
        """
        if not self.load_latest_data(force_download=force_download):
            return []

        anchor_year = self.loaded_year
        anchor_quarter = self.loaded_quarter
        if anchor_year is None or anchor_quarter is None:
            return []

        periods = [
            (cast(int, year), cast(int, quarter))
            for year, quarter in self.recent_periods()
            if year is not None and quarter is not None
        ]
        cached: list[tuple[int, int]] = [periods[0]]
        for period in periods[1:]:
            if self.load_data(*period):
                cached.append(period)

        # A warmup walks backward through the cache. Restore the latest
        # successfully loaded period so default queries always mean latest.
        self.load_data(anchor_year, anchor_quarter)
        return cached

    def load_data(
        self,
        year: int | None = None,
        quarter: int | None = None,
        force_download: bool = False,
    ) -> bool:
        """Load LCA data from cache or download if needed"""
        if year is None and quarter is None:
            return self.load_latest_data(force_download)
        if year is None or quarter is None:
            raise ValueError("year and quarter must be provided together")
        if quarter not in range(1, 5):
            raise ValueError("quarter must be between 1 and 4")

        with self._data_lock:
            # A selected store retains metadata only, never a full quarter.
            if (
                not force_download
                and self.store is not None
                and (self.loaded_year, self.loaded_quarter) == (year, quarter)
            ):
                return True

            # Retain the period lock as a second line of defence for cache-file
            # writes. The data lock also prevents queries from observing a
            # store while another thread switches periods.
            with self._lock_for_period(year, quarter):
                return self._load_period(year, quarter, force_download)

    def _load_period(self, year: int, quarter: int, force_download: bool) -> bool:
        cache_file = os.path.join(DATA_CACHE_DIR, f"LCA_{year}Q{quarter}.sqlite")
        legacy_file = os.path.join(DATA_CACHE_DIR, f"LCA_{year}Q{quarter}.pkl")
        source_url = self.get_dol_urls(year, quarter)[0]
        if not force_download:
            if os.path.exists(cache_file):
                try:
                    self._activate_store(cache_file, year, quarter, source_url)
                    return True
                except Exception as error:
                    print(f"Cannot open indexed cache: {error}")
            if os.path.exists(legacy_file):
                try:
                    self._convert_cache(legacy_file, cache_file)
                    self._activate_store(cache_file, year, quarter, source_url)
                    return True
                except Exception as error:
                    print(f"Cannot migrate legacy cache: {error}")
                    # Preserve the original and retry later; do not download a
                    # second full dataset on top of a failed migration.
                    return False

        # Try downloading from multiple possible URLs
        urls = self.get_dol_urls(year, quarter)
        excel_file = os.path.join(DATA_CACHE_DIR, f"LCA_{year}Q{quarter}.xlsx")
        
        for url in urls:
            try:
                print(f"Attempting to download LCA data from: {url}")
                
                # First try with curl for DOL URLs (more reliable for government sites)
                if "dol.gov" in url:
                    try:
                        print("  Using curl to download from DOL...")
                        # Use curl which handles DOL's security better
                        curl_cmd = [
                            'curl', '-sS', '--fail', '-L', '-o', excel_file,
                            '--max-time', '300',
                            url
                        ]
                        result = subprocess.run(
                            curl_cmd,
                            capture_output=True,
                            text=True,
                            timeout=310,
                            check=False,
                        )

                        if result.returncode != 0:
                            print(f"Curl download failed for {url}: {result.stderr.strip()}")
                            if os.path.exists(excel_file):
                                os.remove(excel_file)
                        
                        # Check if file was downloaded successfully
                        if os.path.exists(excel_file):
                            file_size = os.path.getsize(excel_file)
                            if file_size > 10000:  # At least 10KB
                                print(f"Successfully downloaded {file_size / 1024 / 1024:.1f} MB from {url}")
                            else:
                                print(f"Downloaded file too small ({file_size} bytes)")
                                os.remove(excel_file)
                                continue
                        else:
                            print("Curl download failed - no file created")
                            continue
                            
                    except Exception as e:
                        print(f"Curl failed: {e}, trying requests library...")
                        if os.path.exists(excel_file):
                            os.remove(excel_file)
                        # Fall through to try with requests
                        
                # Try with requests library as fallback or for non-DOL URLs  
                if not os.path.exists(excel_file):
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Accept': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*',
                    }
                    
                    response = requests.get(url, stream=True, timeout=120, headers=headers)
                    response.raise_for_status()
                    
                    # Check if we got an HTML error page
                    content_type = response.headers.get('content-type', '')
                    if 'text/html' in content_type.lower():
                        print(f"Received HTML instead of Excel from {url}, skipping...")
                        continue
                    
                    # Save the file
                    with open(excel_file, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                    
                    print(f"Successfully downloaded from {url}")
                
                # Verify file exists and has content
                if not os.path.exists(excel_file):
                    print(f"Error: Downloaded file not found at {excel_file}")
                    continue
                    
                file_size = os.path.getsize(excel_file)
                print(f"Downloaded file size: {file_size / 1024 / 1024:.1f} MB")
                
                if file_size < 1000:
                    print(f"Error: File too small ({file_size} bytes), likely not valid")
                    os.remove(excel_file)
                    continue

                # DOL can return an HTML error/interstitial with HTTP 200 and
                # a misleadingly large body. An .xlsx file is a ZIP archive;
                # reject invalid payloads before pandas/openpyxl tries to read
                # them and emits "File is not a zip file".
                if not zipfile.is_zipfile(excel_file):
                    print(f"Downloaded payload is not a valid XLSX file from {url}, skipping...")
                    os.remove(excel_file)
                    continue
                
                self._convert_cache(excel_file, cache_file)
                store = self._activate_store(cache_file, year, quarter, url)
                os.remove(excel_file)
                print(f"Data indexed successfully: {len(store)} records")
                return True
                
            except requests.exceptions.RequestException as e:
                print(f"Failed to download from {url}: {e}")
                continue
            except Exception as e:
                print(f"Error processing data from {url}: {e}")
                # Clean up partial download if exists
                if os.path.exists(excel_file):
                    os.remove(excel_file)
                continue
        
        # If all URLs failed, return error
        print(f"ERROR: Could not download LCA data for {year} Q{quarter} from any source")
        print("The DOL website may be under maintenance or the data format may have changed.")
        print("Please check https://www.dol.gov/agencies/eta/foreign-labor/performance for updates.")
        return False

    def ensure_loaded(self) -> bool:
        """Load the latest disclosure period when a fresh process has no data."""
        return self.is_loaded() or self.load_latest_data()

    def ensure_latest_loaded(self) -> bool:
        """Ensure unscoped searches use the newest known disclosure period."""
        latest_period = self.latest_known_period()
        if latest_period is None:
            return self.is_loaded() or self.load_latest_data()
        if self.is_loaded() and (
            self.loaded_year,
            self.loaded_quarter,
        ) == latest_period:
            return True
        return self.load_data(*latest_period)
    
    def is_loaded(self) -> bool:
        return self.store is not None

    def get_loaded_data(self) -> DisclosureStore:
        """Return the selected store; queries open short-lived read connections."""
        if self.store is None:
            raise RuntimeError("H-1B disclosure data is not loaded")
        return self.store

    def period_label(self) -> str | None:
        if self.loaded_year is None or self.loaded_quarter is None:
            return None
        return f"FY{self.loaded_year} Q{self.loaded_quarter}"

    def recent_periods(
        self,
        count: int = RECENT_SPONSORSHIP_QUARTERS,
    ) -> Sequence[tuple[int | None, int | None]]:
        """Return the loaded fiscal period and its preceding quarters."""
        if count < 1 or not self.is_loaded():
            return []

        if self.loaded_year is None or self.loaded_quarter is None:
            return [(None, None)]

        return self.periods_ending_at(self.loaded_year, self.loaded_quarter, count)

    def latest_period_label(self) -> str | None:
        if self.latest_available_year is None or self.latest_available_quarter is None:
            return None
        return f"FY{self.latest_available_year} Q{self.latest_available_quarter}"

data_manager = H1BDataManager()


def serialized_data_access(function):
    """Keep each MCP operation on the manager's selected fiscal period."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with data_manager.operation():
            return function(*args, **kwargs)

    return wrapped


async def _warm_up_data_cache() -> None:
    cached_periods = await asyncio.to_thread(data_manager.cache_recent_data)
    if not cached_periods:
        print(
            "H-1B disclosure data is unavailable at startup; continuing with "
            "the server live so a later load can retry the DOL sources."
        )


@asynccontextmanager
async def server_lifespan(_server):
    # The six-quarter warmup downloads and parses up to six multi-hundred
    # MB DOL spreadsheets, which can take well over Railway's healthcheck
    # timeout. Run it in the background so /health is reachable immediately
    # (reporting "degraded" until the warmup finishes) instead of the whole
    # server being unreachable during warmup.
    warmup_task = asyncio.create_task(_warm_up_data_cache())
    cache_cleanup_task = asyncio.create_task(query_file_cache.run())
    try:
        yield {"data_manager": data_manager}
    finally:
        warmup_task.cancel()
        cache_cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cache_cleanup_task


mcp = FastMCP("H1B Job Search MCP Server", lifespan=server_lifespan)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: Request) -> JSONResponse:
    ready = data_manager.is_loaded()
    return JSONResponse(
        {
            "status": "ok" if ready else "degraded",
            "data_version": data_manager.period_label(),
        },
        status_code=200,
    )

@mcp.tool(
    description=(
        "Download and load H-1B LCA disclosure data from the U.S. Department "
        "of Labor. If year and quarter are omitted, discover and load the "
        "latest available period."
    )
)
@serialized_data_access
def load_h1b_data(
    year: Optional[int] = None,
    quarter: Optional[int] = None,
    force_download: bool = False,
) -> Dict:
    """
    Load H-1B LCA data for analysis.
    
    Args:
        year: Fiscal year. Omit both year and quarter to discover the latest period.
        quarter: Quarter 1-4. Omit both year and quarter to discover the latest period.
        force_download: Force re-download even if cached (default: False)
    
    Returns:
        Status and statistics about the loaded data
    """
    try:
        success = data_manager.load_data(year, quarter, force_download)
    except ValueError as error:
        return {"status": "error", "message": str(error)}
    
    if success:
        df = data_manager.get_loaded_data()
        return {
            "status": "success",
            "records_loaded": len(df),
            "columns": list(df.columns)[:20],
            "year": data_manager.loaded_year,
            "quarter": data_manager.loaded_quarter,
            "cache_file": data_manager.current_file,
            "fiscal_periods": [data_manager.period_label()],
            "data_version": data_manager.period_label(),
            "source_url": data_manager.source_url,
        }
    else:
        return {
            "status": "error",
            "message": "Failed to load data. Check year/quarter or try again."
        }

@mcp.tool(
    description=(
        "Search H-1B sponsoring companies by job role and location. Each "
        "position includes company-level statistics calculated across all "
        "loaded positions for that employer."
    )
)
@serialized_data_access
def search_h1b_jobs(
    job_role: str,
    city: Optional[str] = None,
    state: Optional[str] = None,
    min_wage: Optional[float] = None,
    max_results: int = 50,
    skip_agencies: bool = True
) -> Dict:
    """
    Search for H-1B sponsoring companies.
    
    Args:
        job_role: Job title to search for (partial match)
        city: Work city (optional)
        state: Work state code (optional)
        min_wage: Minimum wage filter (optional)
        max_results: Maximum results to return, from 0 to 1000 (default: 50)
        skip_agencies: Skip staffing agencies (default: True)
    
    Returns:
        List of matching employers with details
    """
    if not data_manager.ensure_latest_loaded():
        return {"error": "H-1B disclosure data could not be loaded."}
    
    return data_manager.get_loaded_data().search(
        job_role, city, state, min_wage, max_results, skip_agencies,
        data_manager.period_label(), data_manager.source_url,
    )

@mcp.tool(
    description=(
        "Get statistics about a company's H-1B sponsorship data for one "
        "fiscal quarter. Defaults to the latest available quarter; pass both "
        "year and quarter to select another cached quarter from the past "
        "six quarters."
    )
)
@serialized_data_access
def get_company_stats(
    company_name: str,
    year: Optional[int] = None,
    quarter: Optional[int] = None,
) -> Dict:
    """
    Get detailed H-1B sponsorship statistics for one fiscal quarter.

    Args:
        company_name: Company name to search for
        year: Optional fiscal year. Must be provided with quarter.
        quarter: Optional quarter from 1 to 4. Must be provided with year.

    Returns:
        Statistics including sponsorship count, job titles, wages
    """
    if (year is None) != (quarter is None):
        return {"error": "year and quarter must be provided together"}
    if quarter is not None and quarter not in range(1, 5):
        return {"error": "quarter must be between 1 and 4"}
    if not data_manager.ensure_loaded():
        return {"error": "H-1B disclosure data could not be loaded."}

    original_period = (
        cast(int, data_manager.loaded_year),
        cast(int, data_manager.loaded_quarter),
    )

    latest_period = data_manager.latest_known_period()
    recent_periods = (
        data_manager.periods_ending_at(*latest_period)
        if latest_period is not None
        else []
    )
    requested_period = (
        (cast(int, year), cast(int, quarter))
        if year is not None and quarter is not None
        else (recent_periods[0] if recent_periods else None)
    )
    if requested_period is not None and requested_period not in recent_periods:
        return {
            "error": (
                "Requested period is outside the cached six-quarter window. "
                "Use get_available_data to list selectable quarters."
            )
        }
    period_label = (
        f"FY{requested_period[0]} Q{requested_period[1]}"
        if requested_period is not None
        else (data_manager.period_label() or "currently loaded data")
    )

    try:
        if requested_period is not None and requested_period != (
            data_manager.loaded_year,
            data_manager.loaded_quarter,
        ) and not data_manager.load_data(*requested_period):
            return {
                "error": f"H-1B disclosure data is unavailable for {period_label}."
            }

        stats = _company_stats_for_loaded_period(company_name, period_label)
        if stats is None:
            return {
                "message": f"No sponsorship data found for {company_name} in {period_label}.",
                "searched_periods": [period_label],
                "fiscal_periods": [period_label],
                "data_version": period_label,
                "latest_sponsorship_period": None,
            }

        stats["searched_periods"] = [period_label]
        stats["latest_sponsorship_period"] = period_label
        return stats
    finally:
        if original_period != (
            data_manager.loaded_year,
            data_manager.loaded_quarter,
        ):
            data_manager.load_data(*original_period)


def _company_stats_for_loaded_period(
    company_name: str,
    period_label: str,
) -> Dict | None:
    """Aggregate on disk, including the median, without collecting company rows."""
    return data_manager.get_loaded_data().company_stats(
        company_name, period_label, data_manager.source_url,
    )


@mcp.tool(
    description=(
        "Return quarterly H-1B filing counts for a company across the cached "
        "six-quarter window. Use this compact series to plot sponsorship volume."
    )
)
@serialized_data_access
def get_company_sponsorship_trend(company_name: str) -> dict:
    """Return newest-first quarterly filing figures for charting."""
    if not data_manager.ensure_loaded():
        return {"error": "H-1B disclosure data could not be loaded."}

    original_period = (
        cast(int, data_manager.loaded_year),
        cast(int, data_manager.loaded_quarter),
    )
    periods: list[dict] = []
    missing_periods: list[str] = []

    latest_period = data_manager.latest_known_period()
    if latest_period is None:
        return {
            "company": company_name,
            "periods": [],
            "missing_periods": [],
            "period_count": 0,
            "window_years": HISTORICAL_CACHE_YEARS,
        }
    try:
        for year, quarter in data_manager.periods_ending_at(*latest_period):
            period_label = f"FY{year} Q{quarter}"
            if (year, quarter) != (
                data_manager.loaded_year,
                data_manager.loaded_quarter,
            ) and not data_manager.load_data(year, quarter):
                missing_periods.append(period_label)
                continue

            stats = _company_stats_for_loaded_period(company_name, period_label)
            if stats is None:
                periods.append(
                    {
                        "period": period_label,
                        "filing_count": 0,
                        "certified_count": 0,
                        "denied_count": 0,
                        "wage_summary": None,
                    }
                )
                continue

            periods.append(
                {
                    "period": period_label,
                    "filing_count": stats["total_applications"],
                    "certified_count": stats.get("certified"),
                    "denied_count": stats.get("denied"),
                    "wage_summary": stats.get("wage_stats"),
                }
            )
    finally:
        if original_period != (
            data_manager.loaded_year,
            data_manager.loaded_quarter,
        ):
            data_manager.load_data(*original_period)

    return {
        "company": company_name,
        "periods": periods,
        "missing_periods": missing_periods,
        "period_count": len(periods),
        "window_years": HISTORICAL_CACHE_YEARS,
    }

@mcp.tool(description="Export filtered H-1B data to CSV file")
@serialized_data_access
def export_results(
    job_role: str,
    city: Optional[str] = None,
    state: Optional[str] = None,
    filename: str = "h1b_results.csv",
    max_results: int = 1000
) -> Dict:
    """
    Export filtered H-1B results to a CSV file.
    
    Args:
        job_role: Job title to filter
        city: City filter (optional)
        state: State filter (optional)
        filename: Output filename (default: h1b_results.csv)
        max_results: Maximum results to export, from 0 to 1000 (default: 1000)
    
    Returns:
        File path and export statistics
    """
    if not data_manager.is_loaded():
        return {"error": "Data not loaded. Please run load_h1b_data first."}
    
    search_results = search_h1b_jobs(
        job_role=job_role,
        city=city,
        state=state,
        max_results=max_results,
        skip_agencies=True
    )
    
    if "error" in search_results:
        return search_results
    
    rows = search_results["results"]
    export_path = os.path.join(DATA_CACHE_DIR, filename)
    with open(export_path, 'w', newline='', encoding='utf-8') as output:
        columns = list(dict.fromkeys(key for row in rows for key in row))
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return {
        "status": "success", "file_path": export_path,
        "records_exported": len(rows), "total_matches": search_results["total_matches"],
    }

@mcp.tool(description="List top H-1B sponsoring companies by volume")
@serialized_data_access
def get_top_sponsors(limit: int = 20, exclude_agencies: bool = True) -> Dict:
    """
    Get top H-1B sponsoring companies by application volume.
    
    Args:
        limit: Number of companies to return, from 0 to 1000 (default: 20)
        exclude_agencies: Exclude staffing agencies (default: True)
    
    Returns:
        List of top sponsoring companies with statistics
    """
    if not data_manager.ensure_latest_loaded():
        return {"error": "Data not loaded. Please run load_h1b_data first."}
    
    return data_manager.get_loaded_data().top_sponsors(limit, exclude_agencies)

@mcp.tool(description="Talk to the H-1B search in simple words - I'll figure out what you want")
@serialized_data_access
def ask(prompt: str) -> Dict:
    """Natural language interface for H-1B job search.
    
    Examples:
    - "Load the latest H-1B data"
    - "Find software engineer jobs in California"
    - "Show me data scientist positions paying over 150k"
    - "Tell me about Google's H-1B sponsorships"
    - "Who are the top H-1B sponsors?"
    - "Export software engineer jobs to a file"
    """
    import re
    
    text = prompt.strip().lower()
    original_prompt = prompt.strip()
    
    # Helper function to extract numbers
    def extract_number(pattern: str, text: str, default: Optional[int] = None) -> Optional[int]:
        match = re.search(pattern, text)
        if match:
            # Remove commas and $ signs, convert to int
            num_str = match.group(1).replace(',', '').replace('$', '').replace('k', '000')
            try:
                return int(float(num_str))
            except (ValueError, OverflowError):
                pass
        return default
    
    # Helper to extract year and quarter
    def extract_year_quarter(text: str) -> tuple[Optional[int], Optional[int]]:
        year = extract_number(r'\b(20\d{2})\b', text)
        quarter_val = extract_number(r'\bq(\d)\b', text)
        if quarter_val is None:
            quarter_val = extract_number(r'quarter\s+(\d)', text)
        return year, quarter_val
    
    # 1. LOAD DATA
    if any(word in text for word in ['load', 'download', 'get', 'fetch']) and \
       any(word in text for word in ['data', 'h-1b', 'h1b', 'lca', 'records']):
        force = 'fresh' in text or 'force' in text or 'new' in text
        year, quarter = extract_year_quarter(text)
        if year is None and quarter is None:
            result = load_h1b_data(force_download=force)
            message = "Checking and loading the latest available H-1B data..."
        elif year is not None and quarter is None:
            result = load_h1b_data(year=year, quarter=4, force_download=force)
            message = f"Loading H-1B data for {year} Q4..."
        elif year is not None and quarter is not None:
            result = load_h1b_data(year=year, quarter=quarter, force_download=force)
            message = f"Loading H-1B data for {year} Q{quarter}..."
        else:
            result = {
                "status": "error",
                "message": "Please provide a fiscal year when specifying a quarter.",
            }
            message = "I could not determine the requested fiscal period."
        return {
            "action": "load_h1b_data",
            "message": message,
            "result": result,
            "suggestions": [
                "Find software engineer jobs",
                "Show me top H-1B sponsors",
                "Search for data scientist positions in California"
            ]
        }
    
    # 2. SEARCH JOBS
    if any(word in text for word in ['find', 'search', 'show', 'look', 'want', 'need']) and \
       any(word in text for word in ['job', 'position', 'role', 'opportunity', 'engineer', 'developer', 
                                      'scientist', 'analyst', 'manager', 'designer', 'architect']):
        
        # Extract job role - common patterns
        job_patterns = [
            (r'software\s+engineer', 'Software Engineer'),
            (r'data\s+scientist', 'Data Scientist'),
            (r'data\s+engineer', 'Data Engineer'),
            (r'data\s+analyst', 'Data Analyst'),
            (r'product\s+manager', 'Product Manager'),
            (r'ml\s+engineer|machine\s+learning\s+engineer', 'Machine Learning Engineer'),
            (r'devops|dev\s+ops', 'DevOps Engineer'),
            (r'backend\s+engineer', 'Backend Engineer'),
            (r'frontend\s+engineer', 'Frontend Engineer'),
            (r'fullstack|full\s+stack', 'Full Stack Developer'),
            (r'ios\s+developer', 'iOS Developer'),
            (r'android\s+developer', 'Android Developer'),
            (r'qa\s+engineer|test\s+engineer', 'QA Engineer'),
            (r'business\s+analyst', 'Business Analyst'),
            (r'project\s+manager', 'Project Manager'),
            (r'ux\s+designer|ui\s+designer', 'UX Designer'),
            (r'cloud\s+engineer', 'Cloud Engineer'),
            (r'security\s+engineer', 'Security Engineer'),
            (r'database\s+admin|dba', 'Database Administrator'),
            (r'network\s+engineer', 'Network Engineer'),
            (r'python\s+developer', 'Python Developer'),
            (r'java\s+developer', 'Java Developer'),
            (r'javascript\s+developer|js\s+developer', 'JavaScript Developer'),
            (r'programmer|developer|engineer', 'Software Engineer'),  # Generic fallback
        ]
        
        job_role = None
        for pattern, title in job_patterns:
            if re.search(pattern, text):
                job_role = title
                break
        
        if not job_role:
            # Try to extract any word before "jobs", "positions", "roles"
            match = re.search(r'(\w+(?:\s+\w+)?)\s+(?:jobs?|positions?|roles?)', text)
            if match:
                job_role = match.group(1).title()
            else:
                job_role = "Software Engineer"  # Default
        
        # Extract location - city and/or state
        city = None
        state = None
        
        # Check for city, state pattern first
        match = re.search(r'in\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\s*,?\s*([A-Z]{2})', original_prompt)
        if match:
            city = match.group(1)
            state = match.group(2)
        else:
            # State codes
            state_match = re.search(r'\b([A-Z]{2})\b', original_prompt)
            if state_match:
                state = state_match.group(1)
            
            # City names
            cities = ['San Francisco', 'New York', 'Los Angeles', 'Seattle', 'Austin', 
                     'Boston', 'Chicago', 'Denver', 'Atlanta', 'Dallas', 'Houston',
                     'San Jose', 'Mountain View', 'Cupertino', 'Redmond', 'Bellevue']
            for c in cities:
                if c.lower() in text:
                    city = c
                    break
        
        # Extract salary
        min_wage = None
        salary_patterns = [
            r'(?:over|above|minimum|at\s+least|paying)\s+\$?(\d+)k',
            r'(?:over|above|minimum|at\s+least|paying)\s+\$?(\d{3,})',
            r'\$(\d+)k',
            r'\$(\d{3,})',
        ]
        for pattern in salary_patterns:
            match = re.search(pattern, text)
            if match:
                num_str = match.group(1)
                if 'k' in text[match.start():match.end()]:
                    min_wage = float(num_str) * 1000
                else:
                    min_wage = float(num_str)
                break
        
        # Check for agency exclusion
        skip_agencies = any(word in text for word in ['no agency', 'no agencies', 'direct hire', 
                                                       'skip agencies', 'not agency', 'no consultancy',
                                                       'no staffing', 'exclude agencies'])
        
        # Determine max results
        max_results = 50
        if 'all' in text:
            max_results = 200
        elif 'top' in text:
            match = re.search(r'top\s+(\d+)', text)
            if match:
                max_results = int(match.group(1))
        
        result = search_h1b_jobs(
            job_role=job_role,
            city=city,
            state=state,
            min_wage=min_wage,
            max_results=max_results,
            skip_agencies=skip_agencies
        )
        
        return {
            "action": "search_h1b_jobs",
            "search_params": {
                "job_role": job_role,
                "city": city,
                "state": state,
                "min_wage": min_wage,
                "skip_agencies": skip_agencies
            },
            "result": result,
            "suggestions": [
                f"Tell me more about {result['results'][0]['employer']}" if result.get('results') else None,
                "Export these results to CSV",
                "Show me different job roles"
            ]
        }
    
    # 3. COMPANY STATS
    if any(word in text for word in ['tell', 'about', 'statistics', 'stats', 'info', 'information']) and \
       any(word in text for word in ['company', 'employer', 'google', 'microsoft', 'amazon', 'apple', 
                                      'meta', 'facebook', 'netflix', 'tesla', 'uber']):
        
        # Extract company name - look for known companies or capitalized words
        company = None
        known_companies = ['Google', 'Microsoft', 'Amazon', 'Apple', 'Meta', 'Facebook', 
                          'Netflix', 'Tesla', 'Uber', 'Airbnb', 'Twitter', 'LinkedIn',
                          'Oracle', 'Salesforce', 'Adobe', 'Intel', 'Nvidia', 'AMD',
                          'IBM', 'Cisco', 'Dell', 'HP', 'VMware', 'Qualcomm']
        
        for c in known_companies:
            if c.lower() in text:
                company = c
                break
        
        if not company:
            # Try to find a capitalized company name
            match = re.search(r"about\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)'?s?", original_prompt)
            if match:
                company = match.group(1)
        
        if company:
            result = get_company_stats(company_name=company)
            return {
                "action": "get_company_stats",
                "company": company,
                "result": result,
                "suggestions": [
                    f"Search for jobs at {company}",
                    "Show me top H-1B sponsors",
                    "Compare with other companies"
                ]
            }
    
    # 4. TOP SPONSORS
    if any(word in text for word in ['top', 'best', 'leading', 'biggest', 'most']) and \
       any(word in text for word in ['sponsor', 'company', 'employer', 'h-1b', 'h1b']):
        
        limit = 20
        match = re.search(r'top\s+(\d+)', text)
        if match:
            limit = int(match.group(1))
        
        exclude_agencies = 'no agency' in text or 'no agencies' in text or 'exclude agencies' in text
        if not exclude_agencies:
            exclude_agencies = True  # Default to excluding agencies
        
        result = get_top_sponsors(limit=limit, exclude_agencies=exclude_agencies)
        return {
            "action": "get_top_sponsors",
            "limit": limit,
            "result": result,
            "suggestions": [
                "Tell me more about the top company",
                "Search for specific job roles",
                "Show me sponsors including agencies"
            ]
        }
    
    # 5. EXPORT RESULTS
    if any(word in text for word in ['export', 'save', 'download', 'csv', 'excel', 'file', 'spreadsheet']):
        
        # Try to extract job role for export
        job_role = "Software Engineer"  # Default
        for pattern, title in [
            (r'software\s+engineer', 'Software Engineer'),
            (r'data\s+scientist', 'Data Scientist'),
            (r'data\s+engineer', 'Data Engineer'),
            (r'product\s+manager', 'Product Manager'),
        ]:
            if re.search(pattern, text):
                job_role = title
                break
        
        # Extract location if mentioned
        city = None
        state = None
        state_match = re.search(r'\b([A-Z]{2})\b', original_prompt)
        if state_match:
            state = state_match.group(1)
        
        # Generate filename
        filename_parts = [job_role.lower().replace(' ', '_')]
        if city:
            filename_parts.append(city.lower().replace(' ', '_'))
        if state:
            filename_parts.append(state.lower())
        filename = '_'.join(filename_parts) + '_h1b.csv'
        
        result = export_results(
            job_role=job_role,
            city=city,
            state=state,
            filename=filename
        )
        
        return {
            "action": "export_results",
            "filename": filename,
            "result": result,
            "suggestions": [
                "Search for different roles",
                "Filter by location",
                "Show me top sponsors"
            ]
        }
    
    # 6. CHECK AVAILABLE DATA
    if any(word in text for word in ['available', 'check', 'what', 'which']) and \
       any(word in text for word in ['data', 'year', 'quarter', 'period']):
        
        result = get_available_data()
        return {
            "action": "get_available_data",
            "result": result,
            "suggestions": [
                f"Use the loaded {result.get('loaded_period', 'H-1B')} data",
                "Search for jobs",
                "Show me top sponsors"
            ]
        }
    
    # DEFAULT: Show helpful suggestions
    return {
        "action": "help",
        "message": "I can help you search for H-1B sponsoring companies! Here's what you can ask:",
        "examples": [
            "Load the latest H-1B data",
            "Find software engineer jobs in California",
            "Show me data scientist positions paying over 150k",
            "Tell me about Google's H-1B sponsorships",
            "Who are the top 20 H-1B sponsors?",
            "Export Python developer jobs to CSV"
        ],
        "suggestions": [
            "Load the latest H-1B data",
            "Search for your dream job",
            "Check top H-1B sponsors"
        ]
    }

@mcp.tool(description="Get available LCA data years and quarters")
@serialized_data_access
def get_available_data() -> Dict:
    """
    List available LCA data periods and cached files.
    
    Returns:
        Available years, quarters, and cached files
    """
    cached_periods = data_manager.get_cached_periods()
    cached_files = [
        name for name in sorted(os.listdir(DATA_CACHE_DIR))
        if re.fullmatch(r"LCA_\d{4}Q[1-4]\.(?:pkl|sqlite)", name)
    ]

    # Startup normally performs this check already, but make the inspection
    # tool useful when called directly against a fresh manager as well.
    if data_manager.latest_period_label() is None:
        data_manager.discover_latest_period()

    return {
        "loaded_period": data_manager.period_label(),
        "available_periods": [
            f"FY{year} Q{quarter}" for year, quarter in cached_periods
        ],
        "cached_files": cached_files,
        "cache_directory": DATA_CACHE_DIR,
        "source_url": data_manager.source_url,
        "latest_period": data_manager.latest_period_label(),
        "cache_window_years": HISTORICAL_CACHE_YEARS,
        "note": "LCA data is typically available with a 1-quarter delay"
    }

if __name__ == "__main__":
    # ``PORT`` remains the deployment-compatible fallback.  The namespaced
    # overrides let a local launcher run this server beside another web API
    # without colliding with that API's PORT/HOST settings.
    port = int(os.environ.get("H1B_PORT", os.environ.get("PORT", 8000)))
    host = os.environ.get("H1B_HOST", "0.0.0.0")
    
    print(f"Starting H1B Job Search MCP Server on {host}:{port}")
    print("Available tools:")
    print("- load_h1b_data: Download and load LCA data")
    print("- search_h1b_jobs: Search for H-1B sponsoring companies")
    print("- get_company_stats: Get company sponsorship statistics")
    print("- get_top_sponsors: List top H-1B sponsors")
    print("- export_results: Export search results to CSV")
    print("- get_available_data: Check available data periods")
    
    mcp.run(
        transport="http",
        host=host,
        port=port,
        stateless_http=True
    )
