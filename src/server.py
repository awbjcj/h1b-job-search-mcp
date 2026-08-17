#!/usr/bin/env python3
import os
import asyncio
import re
import pandas as pd
import requests
import subprocess
import unicodedata
from contextlib import asynccontextmanager
from io import StringIO
from typing import Any, Dict, Optional, cast
from datetime import datetime
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

DATA_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data_cache")
os.makedirs(DATA_CACHE_DIR, exist_ok=True)
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
LATEST_DISCOVERY_LOOKBACK_YEARS = 5
RECENT_SPONSORSHIP_QUARTERS = 4

# These forms occur frequently in the FY2026 Q1 employer data. Canonicalizing
# dotted abbreviations before removing legal suffixes keeps names such as
# "Woven by Toyota, U.S., Inc." and "WOVEN BY TOYOTA US INC" equivalent.
_EMPLOYER_ABBREVIATIONS = (
    (("p", "l", "l", "c"), "pllc"),
    (("g", "m", "b", "h"), "gmbh"),
    (("l", "l", "c"), "llc"),
    (("l", "l", "p"), "llp"),
    (("p", "l", "c"), "plc"),
    (("u", "s", "a"), "usa"),
    (("u", "s"), "us"),
    (("n", "a"), "na"),
    (("p", "c"), "pc"),
    (("p", "a"), "pa"),
    (("l", "p"), "lp"),
    (("s", "a"), "sa"),
    (("a", "g"), "ag"),
    (("b", "v"), "bv"),
    (("n", "v"), "nv"),
)
_EMPLOYER_LEGAL_SUFFIX_PHRASES = (
    ("limited", "liability", "company"),
    ("public", "benefit", "corporation"),
    ("professional", "corporation"),
)
_EMPLOYER_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "llp",
        "lp",
        "pllc",
        "plc",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "pc",
        "pa",
        "pbc",
        "na",
        "sa",
        "ag",
        "bv",
        "nv",
        "gmbh",
    }
)

class H1BDataManager:
    def __init__(self):
        self.df: pd.DataFrame | None = None
        self.last_loaded = None
        self.current_file = None
        self.loaded_year = None
        self.loaded_quarter = None
        self.source_url = None
        self.latest_available_year = None
        self.latest_available_quarter = None
        self.latest_checked = None
        self.discovered_periods: list[tuple[int, int]] = []
        
    def get_dol_urls(self, year: int, quarter: int) -> list:
        """Generate DOL URLs based on actual file naming patterns from the DOL website"""
        urls = []
        
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
        
        return urls
    
    def get_cached_periods(self) -> list[tuple[int, int]]:
        """Return cached fiscal periods ordered from oldest to newest."""
        if not os.path.exists(DATA_CACHE_DIR):
            return []

        periods = set()
        for file_name in os.listdir(DATA_CACHE_DIR):
            match = re.fullmatch(r"LCA_(\d{4})Q([1-4])\.pkl", file_name)
            if match:
                periods.add((int(match.group(1)), int(match.group(2))))
        return sorted(periods)

    def newest_cached_period(self) -> tuple[int, int] | None:
        """Return the newest cached fiscal period, if one exists."""
        periods = self.get_cached_periods()
        return periods[-1] if periods else None

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
            periods = {
                (int(year), int(quarter))
                for year, quarter in LCA_DISCLOSURE_PATTERN.findall(response.text)
            }
            if periods:
                self.discovered_periods = sorted(periods, reverse=True)
                return self._record_latest_period(max(periods))
            print("DOL performance page did not list any LCA disclosure files")
        except requests.exceptions.RequestException as error:
            print(f"Could not read the DOL performance page: {error}")
        except Exception as error:
            print(f"Could not parse the DOL performance page: {error}")

        self.discovered_periods = []
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
                return True

        return False

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

        cache_file = os.path.join(DATA_CACHE_DIR, f"LCA_{year}Q{quarter}.pkl")
        
        # Try loading from cache first
        if not force_download and os.path.exists(cache_file):
            try:
                cached_data = pd.read_pickle(cache_file)
                if not isinstance(cached_data, pd.DataFrame):
                    raise TypeError("Cached H-1B data is not a DataFrame")
                self.df = cached_data
                self.current_file = cache_file
                self.last_loaded = datetime.now()
                self.loaded_year = year
                self.loaded_quarter = quarter
                self.source_url = self.get_dol_urls(year, quarter)[0]
                print(f"Loaded cached data from {cache_file}")
                return True
            except Exception as e:
                print(f"Error loading cached data: {e}")
        
        # Try downloading from multiple possible URLs
        urls = self.get_dol_urls(year, quarter)
        excel_file = os.path.join(DATA_CACHE_DIR, f"LCA_{year}Q{quarter}.xlsx")
        
        for url in urls:
            try:
                print(f"Attempting to download LCA data from: {url}")
                
                # First try with curl for DOL URLs (more reliable for government sites)
                if "dol.gov" in url:
                    try:
                        print(f"  Using curl to download from DOL...")
                        # Use curl which handles DOL's security better
                        curl_cmd = [
                            'curl', '-s', '-L', '-o', excel_file,
                            '--max-time', '300',
                            url
                        ]
                        result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=310)
                        
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
                            print(f"Curl download failed - no file created")
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
                
                # Read the Excel file (limit rows for performance)
                print(f"Reading Excel file with pandas...")
                try:
                    # Use openpyxl engine for .xlsx files
                    self.df = pd.read_excel(excel_file, engine='openpyxl', nrows=100000)
                except Exception as read_error:
                    print(f"Failed to read Excel with openpyxl: {read_error}")
                    # Try without specifying engine as fallback
                    try:
                        self.df = pd.read_excel(excel_file, nrows=100000)
                    except Exception as fallback_error:
                        print(f"Failed to read Excel file: {fallback_error}")
                        os.remove(excel_file)
                        continue
                
                # Cache the processed data
                self.df.to_pickle(cache_file)
                self.current_file = cache_file
                self.last_loaded = datetime.now()
                self.loaded_year = year
                self.loaded_quarter = quarter
                self.source_url = url
                
                # Clean up Excel file to save space
                if os.path.exists(excel_file):
                    os.remove(excel_file)
                
                print(f"Data loaded successfully: {len(self.df)} records")
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
    
    def is_loaded(self) -> bool:
        return self.df is not None

    def get_loaded_data(self) -> pd.DataFrame:
        """Return the disclosure data after enforcing the loaded-state invariant."""
        if self.df is None:
            raise RuntimeError("H-1B disclosure data is not loaded")
        return self.df

    def period_label(self) -> str | None:
        if self.loaded_year is None or self.loaded_quarter is None:
            return None
        return f"FY{self.loaded_year} Q{self.loaded_quarter}"

    def recent_periods(
        self,
        count: int = RECENT_SPONSORSHIP_QUARTERS,
    ) -> list[tuple[int | None, int | None]]:
        """Return the loaded fiscal period and its preceding quarters."""
        if count < 1 or not self.is_loaded():
            return []

        if self.loaded_year is None or self.loaded_quarter is None:
            return [(None, None)]

        periods: list[tuple[int | None, int | None]] = []
        year = self.loaded_year
        quarter = self.loaded_quarter
        for _ in range(count):
            periods.append((year, quarter))
            quarter -= 1
            if quarter == 0:
                year -= 1
                quarter = 4
        return periods

    def latest_period_label(self) -> str | None:
        if self.latest_available_year is None or self.latest_available_quarter is None:
            return None
        return f"FY{self.latest_available_year} Q{self.latest_available_quarter}"

data_manager = H1BDataManager()


@asynccontextmanager
async def server_lifespan(_server):
    loaded = await asyncio.to_thread(data_manager.ensure_loaded)
    if not loaded:
        print(
            "H-1B disclosure data is unavailable at startup; continuing with "
            "the server live so a later load can retry the DOL sources."
        )
    yield {"data_manager": data_manager}


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

def _get_employer_column(df: pd.DataFrame) -> str:
    if "EMPLOYER_NAME" in df.columns:
        return "EMPLOYER_NAME"
    if "EMPLOYER_BUSINESS_DBA" in df.columns:
        return "EMPLOYER_BUSINESS_DBA"
    raise KeyError("Loaded H-1B data has no employer column")


def _employer_tokens(value: Any) -> list[str]:
    if pd.isna(value):
        return []

    text = unicodedata.normalize("NFKD", str(value)).casefold()
    text = "".join(
        character for character in text if not unicodedata.combining(character)
    )
    tokens = re.findall(r"[a-z0-9]+", text.replace("&", " and "))

    canonical_tokens: list[str] = []
    index = 0
    while index < len(tokens):
        for source, replacement in _EMPLOYER_ABBREVIATIONS:
            if tuple(tokens[index : index + len(source)]) == source:
                canonical_tokens.append(replacement)
                index += len(source)
                break
        else:
            canonical_tokens.append(tokens[index])
            index += 1

    return canonical_tokens


def _normalise_employer(value: Any) -> str:
    tokens = _employer_tokens(value)
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    original_tokens = tokens.copy()

    while len(tokens) > 1:
        removed_suffix = False
        for suffix in _EMPLOYER_LEGAL_SUFFIX_PHRASES:
            if len(tokens) > len(suffix) and tuple(tokens[-len(suffix) :]) == suffix:
                del tokens[-len(suffix) :]
                removed_suffix = True
                break
        if removed_suffix:
            continue
        if tokens[-1] in _EMPLOYER_LEGAL_SUFFIXES:
            tokens.pop()
            continue
        break

    if not tokens:
        tokens = original_tokens
    return "".join(tokens)


def _filter_company_rows(
    df: pd.DataFrame,
    employer_col: str,
    company_name: str,
) -> pd.DataFrame:
    company_key = _normalise_employer(company_name)
    if not company_key:
        return df.iloc[0:0]

    employer_keys = df[employer_col].map(_normalise_employer)
    match_mask = employer_keys.str.contains(company_key, regex=False, na=False)
    return cast(pd.DataFrame, df.loc[match_mask])


def _python_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _build_company_stats(
    company_df: pd.DataFrame,
    employer_col: str,
    *,
    period_label: str | None = None,
    source_url: str | None = None,
) -> Dict:
    """Calculate statistics across every loaded position for one company."""
    if company_df.empty:
        return {}

    selected_period = (
        period_label if period_label is not None else data_manager.period_label()
    )
    selected_source_url = (
        source_url if source_url is not None else data_manager.source_url
    )

    job_col = next(
        (column for column in ["JOB_TITLE", "SOC_TITLE", "JOB_TITLE_CLEAN"] if column in company_df.columns),
        None,
    )
    wage_col = next(
        (
            column
            for column in [
                "WAGE_RATE_OF_PAY_FROM",
                "PREVAILING_WAGE",
                "WAGE_RATE_OF_PAY",
            ]
            if column in company_df.columns
        ),
        None,
    )

    stats = {
        "company": company_df[employer_col].iloc[0],
        "total_applications": int(len(company_df)),
        "certified": (
            int(
                company_df["CASE_STATUS"]
                .astype(str)
                .str.casefold()
                .eq("certified")
                .sum()
            )
            if "CASE_STATUS" in company_df.columns
            else "N/A"
        ),
        "fiscal_periods": [selected_period],
        "data_version": selected_period,
        "source_url": selected_source_url,
    }

    if isinstance(stats["certified"], int):
        stats["certification_rate"] = round(
            stats["certified"] / len(company_df) * 100,
            2,
        )

    if job_col:
        stats["top_job_titles"] = {
            str(job_title): int(count)
            for job_title, count in company_df[job_col].value_counts().head(10).items()
        }

    if wage_col:
        wages = pd.to_numeric(company_df[wage_col], errors="coerce")
        stats["wage_stats"] = {
            "min": _python_value(wages.min()),
            "max": _python_value(wages.max()),
            "mean": _python_value(wages.mean()),
            "median": _python_value(wages.median()),
        }

    if "WORKSITE_STATE" in company_df.columns:
        stats["top_states"] = {
            str(state): int(count)
            for state, count in company_df["WORKSITE_STATE"].value_counts().head(5).items()
        }

    return stats


@mcp.tool(
    description=(
        "Search H-1B sponsoring companies by job role and location. Each "
        "position includes company-level statistics calculated across all "
        "loaded positions for that employer."
    )
)
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
        max_results: Maximum results to return (default: 50)
        skip_agencies: Skip staffing agencies (default: True)
    
    Returns:
        List of matching employers with details
    """
    if not data_manager.ensure_loaded():
        return {"error": "H-1B disclosure data could not be loaded."}
    
    all_df = data_manager.get_loaded_data().copy()
    df = all_df.copy()

    employer_col = _get_employer_column(all_df)
    
    job_columns = ['JOB_TITLE', 'SOC_TITLE', 'JOB_TITLE_CLEAN']
    job_col = None
    for col in job_columns:
        if col in df.columns:
            job_col = col
            break
    
    if job_col:
        df = df[df[job_col].str.contains(job_role, case=False, na=False)]
    
    if city and 'WORKSITE_CITY' in df.columns:
        df = df[df['WORKSITE_CITY'].str.contains(city, case=False, na=False)]
    elif city and 'EMPLOYER_CITY' in df.columns:
        df = df[df['EMPLOYER_CITY'].str.contains(city, case=False, na=False)]
    
    if state:
        if 'WORKSITE_STATE' in df.columns:
            df = df[df['WORKSITE_STATE'].str.upper() == state.upper()]
        elif 'EMPLOYER_STATE' in df.columns:
            df = df[df['EMPLOYER_STATE'].str.upper() == state.upper()]
    
    wage_col = None
    for col in ['WAGE_RATE_OF_PAY_FROM', 'PREVAILING_WAGE', 'WAGE_RATE_OF_PAY']:
        if col in df.columns:
            wage_col = col
            break
    
    if min_wage and wage_col:
        df[wage_col] = pd.to_numeric(df[wage_col], errors='coerce')
        df = df[df[wage_col] >= min_wage]
    
    if skip_agencies and 'EMPLOYER_NAME' in df.columns:
        agency_keywords = [
            'staffing', 'consulting', 'agency', 'infosys', 'tcs', 
            'wipro', 'cognizant', 'hcl', 'tech mahindra', 'accenture'
        ]
        mask = ~df['EMPLOYER_NAME'].str.contains('|'.join(agency_keywords), case=False, na=False)
        df = df[mask]
    
    status_col = 'CASE_STATUS' if 'CASE_STATUS' in df.columns else None
    if status_col:
        df = df[df[status_col].astype(str).str.casefold() == 'certified']
    
    all_employer_keys = all_df[employer_col].map(_normalise_employer)
    company_stats_by_key = {}
    for employer in df[employer_col].dropna().unique():
        employer_key = _normalise_employer(employer)
        company_stats_by_key[employer_key] = _build_company_stats(
            all_df[all_employer_keys == employer_key],
            employer_col,
        )
    
    results = []
    for _, row in df.head(max_results).iterrows():
        result = {
            "employer": row.get(employer_col, "Unknown"),
            "job_title": row.get(job_col, "Unknown"),
            "city": row.get('WORKSITE_CITY', row.get('EMPLOYER_CITY', "Unknown")),
            "state": row.get('WORKSITE_STATE', row.get('EMPLOYER_STATE', "Unknown")),
        }

        company_stats = company_stats_by_key.get(
            _normalise_employer(row.get(employer_col))
        )
        if company_stats:
            result["company_stats"] = company_stats
        
        if wage_col:
            result["wage"] = row.get(wage_col, "N/A")
        
        contact_fields = ['EMPLOYER_POC_EMAIL', 'CONTACT_EMAIL', 'EMPLOYER_PHONE']
        for field in contact_fields:
            if field in row and pd.notna(row[field]):
                result["contact"] = row[field]
                break
        
        results.append(result)
    
    return {
        "total_matches": len(df),
        "returned": len(results),
        "results": results,
        "fiscal_periods": [data_manager.period_label()],
        "data_version": data_manager.period_label(),
        "source_url": data_manager.source_url,
    }

@mcp.tool(
    description=(
        "Get statistics about a company's latest H-1B sponsorship data, "
        "searching the most recent four fiscal quarters."
    )
)
def get_company_stats(company_name: str) -> Dict:
    """
    Get detailed H-1B sponsorship statistics for the latest matching quarter.

    Args:
        company_name: Company name to search for

    Returns:
        Statistics including sponsorship count, job titles, wages
    """
    if not data_manager.ensure_loaded():
        return {"error": "H-1B disclosure data could not be loaded."}

    original_df = data_manager.df
    original_last_loaded = data_manager.last_loaded
    original_current_file = data_manager.current_file
    original_loaded_year = data_manager.loaded_year
    original_loaded_quarter = data_manager.loaded_quarter
    original_source_url = data_manager.source_url

    periods = data_manager.recent_periods()
    searched_periods: list[str] = []

    try:
        for year, quarter in periods:
            if year is None or quarter is None:
                period_label = data_manager.period_label()
                display_period = period_label or "currently loaded data"
                period_df = data_manager.get_loaded_data()
            elif (year, quarter) == (original_loaded_year, original_loaded_quarter):
                period_label = f"FY{year} Q{quarter}"
                display_period = period_label
                period_df = data_manager.get_loaded_data()
            else:
                period_label = f"FY{year} Q{quarter}"
                display_period = period_label
                if not data_manager.load_data(year, quarter):
                    searched_periods.append(display_period)
                    continue
                period_df = data_manager.get_loaded_data()

            searched_periods.append(display_period)
            employer_col = _get_employer_column(period_df)
            company_df = _filter_company_rows(
                period_df,
                employer_col,
                company_name,
            )
            if company_df.empty:
                continue

            stats = _build_company_stats(
                company_df.copy(),
                employer_col,
                period_label=period_label,
                source_url=data_manager.source_url,
            )
            stats["searched_periods"] = searched_periods
            stats["latest_sponsorship_period"] = period_label
            return stats
    finally:
        data_manager.df = original_df
        data_manager.last_loaded = original_last_loaded
        data_manager.current_file = original_current_file
        data_manager.loaded_year = original_loaded_year
        data_manager.loaded_quarter = original_loaded_quarter
        data_manager.source_url = original_source_url

    return {
        "message": (
            f"No recent sponsorship data found for {company_name}. "
            "Searched the latest four fiscal quarters: "
            f"{', '.join(searched_periods)}."
        ),
        "searched_periods": searched_periods,
    }

@mcp.tool(description="Export filtered H-1B data to CSV file")
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
        max_results: Maximum results to export (default: 1000)
    
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
    
    df_export = pd.DataFrame(search_results["results"])
    
    export_path = os.path.join(DATA_CACHE_DIR, filename)
    df_export.to_csv(export_path, index=False)
    
    return {
        "status": "success",
        "file_path": export_path,
        "records_exported": len(df_export),
        "total_matches": search_results["total_matches"]
    }

@mcp.tool(description="List top H-1B sponsoring companies by volume")
def get_top_sponsors(limit: int = 20, exclude_agencies: bool = True) -> Dict:
    """
    Get top H-1B sponsoring companies by application volume.
    
    Args:
        limit: Number of companies to return (default: 20)
        exclude_agencies: Exclude staffing agencies (default: True)
    
    Returns:
        List of top sponsoring companies with statistics
    """
    if not data_manager.is_loaded():
        return {"error": "Data not loaded. Please run load_h1b_data first."}
    
    df = data_manager.get_loaded_data().copy()
    
    employer_col = 'EMPLOYER_NAME' if 'EMPLOYER_NAME' in df.columns else 'EMPLOYER_BUSINESS_DBA'
    
    if exclude_agencies:
        agency_keywords = [
            'staffing', 'consulting', 'agency', 'infosys', 'tcs',
            'wipro', 'cognizant', 'hcl', 'tech mahindra', 'accenture'
        ]
        mask = ~df[employer_col].str.contains('|'.join(agency_keywords), case=False, na=False)
        df = df[mask]
    
    top_companies = df[employer_col].value_counts().head(limit)
    
    results = []
    for company, count in top_companies.items():
        company_df = df[df[employer_col] == company]
        
        wage_col = None
        for col in ['WAGE_RATE_OF_PAY_FROM', 'PREVAILING_WAGE', 'WAGE_RATE_OF_PAY']:
            if col in df.columns:
                wage_col = col
                company_df[wage_col] = pd.to_numeric(company_df[wage_col], errors='coerce')
                break
        
        result = {
            "company": company,
            "total_applications": count,
            "certified": len(company_df[company_df.get('CASE_STATUS', '') == 'CERTIFIED']) if 'CASE_STATUS' in company_df.columns else count,
        }
        
        if wage_col:
            result["avg_wage"] = company_df[wage_col].mean()
        
        if 'WORKSITE_STATE' in company_df.columns:
            result["primary_state"] = company_df['WORKSITE_STATE'].mode()[0] if len(company_df['WORKSITE_STATE'].mode()) > 0 else "N/A"
        
        results.append(result)
    
    return {
        "top_sponsors": results,
        "total_companies": df[employer_col].nunique()
    }

@mcp.tool(description="Talk to the H-1B search in simple words - I'll figure out what you want")
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
            except:
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
        
        # Common city patterns
        city_patterns = [
            r'in\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*,?\s*([A-Z]{2})',  # City, State
            r'(?:in|at|near)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',  # City name
        ]
        
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
def get_available_data() -> Dict:
    """
    List available LCA data periods and cached files.
    
    Returns:
        Available years, quarters, and cached files
    """
    cached_periods = data_manager.get_cached_periods()
    cached_files = [
        f"LCA_{year}Q{quarter}.pkl" for year, quarter in cached_periods
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
