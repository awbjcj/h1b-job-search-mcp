import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)

from starlette.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import server  # noqa: E402


def disclosure_rows(*, case_status: str = "Certified") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "CASE_STATUS": case_status,
                "EMPLOYER_NAME": "Google LLC",
                "JOB_TITLE": "Software Engineer",
                "WORKSITE_CITY": "Mountain View",
                "WORKSITE_STATE": "CA",
                "WAGE_RATE_OF_PAY_FROM": 180_000,
            }
        ]
    )


class H1BServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_cache_dir = server.DATA_CACHE_DIR
        self._original_data_manager = server.data_manager
        self._temp_dir = tempfile.TemporaryDirectory()
        server.DATA_CACHE_DIR = self._temp_dir.name
        server.data_manager = server.H1BDataManager()
        # Keep unit tests offline unless a test explicitly exercises DOL
        # discovery.
        self._discover_latest_period_mock = Mock(return_value=None)
        server.data_manager.discover_latest_period = self._discover_latest_period_mock
        # get_company_stats now walks every recent quarter (not just until
        # the first match), so any period a test doesn't explicitly cache
        # must fail fast instead of making a real network call.
        self._no_network_patchers = [
            patch.object(
                server.requests,
                "get",
                side_effect=RuntimeError("Unit tests must not perform real network requests"),
            ),
            patch.object(
                server.subprocess,
                "run",
                side_effect=RuntimeError("Unit tests must not perform real network requests"),
            ),
        ]
        for patcher in self._no_network_patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in self._no_network_patchers:
            patcher.stop()
        server.DATA_CACHE_DIR = self._original_cache_dir
        server.data_manager = self._original_data_manager
        self._temp_dir.cleanup()

    def cache_default_disclosure(self, data: pd.DataFrame | None = None) -> None:
        disclosure = data if data is not None else disclosure_rows()
        self.cache_period(2024, 4, disclosure)

    def cache_period(self, year: int, quarter: int, data: pd.DataFrame) -> None:
        data.to_pickle(
            Path(self._temp_dir.name) / f"LCA_{year}Q{quarter}.pkl"
        )

    def cache_disclosure(self, year: int, quarter: int) -> None:
        self.cache_period(year, quarter, disclosure_rows())

    def test_company_stats_loads_default_cache_on_first_read(self) -> None:
        self.cache_default_disclosure()

        result = server.get_company_stats("Google")

        self.assertNotIn("error", result)
        self.assertEqual(result["total_applications"], 1)

    def test_company_stats_defaults_to_latest_quarter_without_falling_back(self) -> None:
        self.cache_period(2026, 1, disclosure_rows())
        self.cache_period(
            2025,
            4,
            disclosure_rows().assign(
                EMPLOYER_NAME="Woven by Toyota, U.S., Inc."
            ),
        )
        self._discover_latest_period_mock.return_value = (2026, 1)

        result = server.get_company_stats("WOVEN BY TOYOTA US INC")

        self.assertIn("No sponsorship data", result["message"])
        self.assertEqual(result["data_version"], "FY2026 Q1")
        self.assertEqual(result["fiscal_periods"], ["FY2026 Q1"])
        self.assertEqual(result["searched_periods"], ["FY2026 Q1"])
        self.assertEqual(server.data_manager.period_label(), "FY2026 Q1")

    def test_company_stats_accepts_an_explicit_cached_quarter(self) -> None:
        self.cache_period(2026, 1, disclosure_rows())
        self.cache_period(
            2025,
            4,
            disclosure_rows().assign(EMPLOYER_NAME="Woven by Toyota, U.S., Inc."),
        )
        self._discover_latest_period_mock.return_value = (2026, 1)

        result = server.get_company_stats(
            "WOVEN BY TOYOTA US INC",
            year=2025,
            quarter=4,
        )

        self.assertEqual(result["company"], "Woven by Toyota, U.S., Inc.")
        self.assertEqual(result["total_applications"], 1)
        self.assertEqual(result["data_version"], "FY2025 Q4")
        self.assertEqual(result["searched_periods"], ["FY2025 Q4"])
        self.assertEqual(server.data_manager.period_label(), "FY2026 Q1")

    def test_company_stats_default_stays_latest_after_an_older_manual_load(self) -> None:
        self.cache_period(2026, 1, disclosure_rows())
        self.cache_period(2025, 4, disclosure_rows())

        self.assertTrue(server.data_manager.load_data(2025, 4))
        result = server.get_company_stats("Google")

        self.assertEqual(result["data_version"], "FY2026 Q1")
        self.assertEqual(result["searched_periods"], ["FY2026 Q1"])
        self.assertEqual(server.data_manager.period_label(), "FY2025 Q4")

    def test_employer_normalization_handles_common_legal_name_forms(self) -> None:
        self.assertEqual(
            server._normalise_employer("Woven by Toyota, U.S., Inc."),
            "wovenbytoyotaus",
        )
        self.assertEqual(
            server._normalise_employer("WOVEN BY TOYOTA US INC"),
            "wovenbytoyotaus",
        )
        self.assertEqual(
            server._normalise_employer("The Example, L.L.C."),
            "example",
        )
        self.assertEqual(
            server._normalise_employer("Example Limited Liability Company"),
            "example",
        )

    def test_company_stats_rejects_a_period_outside_three_year_window(self) -> None:
        self.cache_period(2026, 1, disclosure_rows())
        self._discover_latest_period_mock.return_value = (2026, 1)

        result = server.get_company_stats("Google", year=2023, quarter=1)

        self.assertIn("outside the cached three-year window", result["error"])

    def test_recent_periods_cover_three_fiscal_years(self) -> None:
        server.data_manager.df = disclosure_rows()
        server.data_manager.loaded_year = 2026
        server.data_manager.loaded_quarter = 2

        self.assertEqual(
            server.data_manager.recent_periods(),
            [
                (2026, 2), (2026, 1),
                (2025, 4), (2025, 3), (2025, 2), (2025, 1),
                (2024, 4), (2024, 3), (2024, 2), (2024, 1),
                (2023, 4), (2023, 3),
            ],
        )

    def test_startup_cache_warms_twelve_quarters_and_restores_latest(self) -> None:
        manager = server.H1BDataManager()

        def load_latest(*, force_download=False):
            manager.df = disclosure_rows()
            manager.loaded_year = 2026
            manager.loaded_quarter = 2
            return True

        loaded: list[tuple[int, int]] = []

        def load_period(year, quarter, force_download=False):
            loaded.append((year, quarter))
            manager.df = disclosure_rows()
            manager.loaded_year = year
            manager.loaded_quarter = quarter
            return True

        with patch.object(manager, "load_latest_data", side_effect=load_latest), patch.object(
            manager,
            "load_data",
            side_effect=load_period,
        ):
            cached = manager.cache_recent_data()

        self.assertEqual(len(cached), 12)
        self.assertEqual(loaded[:-1], manager.recent_periods()[1:])
        self.assertEqual(loaded[-1], (2026, 2))
        self.assertEqual(manager.period_label(), "FY2026 Q2")

    def test_company_sponsorship_trend_returns_all_cached_quarters(self) -> None:
        periods = [
            (2026, 1), (2025, 4), (2025, 3), (2025, 2),
            (2025, 1), (2024, 4), (2024, 3), (2024, 2),
            (2024, 1), (2023, 4), (2023, 3), (2023, 2),
        ]
        for year, quarter in periods:
            rows = disclosure_rows()
            if (year, quarter) == (2025, 4):
                rows = pd.concat([rows, disclosure_rows(case_status="Denied")])
            self.cache_period(year, quarter, rows)
        self._discover_latest_period_mock.return_value = (2026, 1)

        result = server.get_company_sponsorship_trend("Google")

        self.assertEqual(result["period_count"], 12)
        self.assertEqual(result["window_years"], 3)
        self.assertEqual(result["missing_periods"], [])
        self.assertEqual(result["periods"][0]["period"], "FY2026 Q1")
        q4 = next(row for row in result["periods"] if row["period"] == "FY2025 Q4")
        self.assertEqual(q4["filing_count"], 2)
        self.assertEqual(q4["certified_count"], 1)
        self.assertEqual(q4["denied_count"], 1)

    def test_loaded_data_accessor_rejects_unloaded_manager(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "data is not loaded"):
            server.data_manager.get_loaded_data()

    def test_load_h1b_data_reports_cached_frame_details(self) -> None:
        self.cache_default_disclosure()

        result = server.load_h1b_data()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["records_loaded"], 1)
        self.assertIn("CASE_STATUS", result["columns"])

    def test_downloaded_quarter_is_cached_without_a_row_limit(self) -> None:
        response = Mock()
        response.headers = {
            "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        }
        response.raise_for_status.return_value = None
        response.iter_content.return_value = [b"x" * 2_000]
        manager = server.H1BDataManager()

        with patch.object(
            manager,
            "get_dol_urls",
            return_value=["https://example.test/LCA_2026Q1.xlsx"],
        ), patch.object(server.requests, "get", return_value=response), patch.object(
            server.pd,
            "read_excel",
            return_value=disclosure_rows(),
        ) as read_excel:
            loaded = manager.load_data(2026, 1)

        self.assertTrue(loaded)
        self.assertNotIn("nrows", read_excel.call_args.kwargs)
        self.assertTrue((Path(self._temp_dir.name) / "LCA_2026Q1.pkl").exists())

    def test_latest_load_uses_newest_cache_when_dol_is_unavailable(self) -> None:
        self.cache_default_disclosure()
        self.cache_disclosure(2025, 4)

        result = server.load_h1b_data()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data_version"], "FY2025 Q4")

    def test_latest_load_uses_dol_discovered_period(self) -> None:
        self.cache_disclosure(2026, 2)
        self._discover_latest_period_mock.return_value = (2026, 2)

        result = server.load_h1b_data()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data_version"], "FY2026 Q2")

    def test_get_dol_urls_includes_dol_media_host(self) -> None:
        urls = server.H1BDataManager().get_dol_urls(2026, 2)

        self.assertTrue(
            any(url.startswith("https://www.dol.gov/media/") for url in urls),
            "DOL has started hosting the newest LCA file under dol.gov/media/; "
            "get_dol_urls must offer that host as a candidate.",
        )

    def test_latest_load_falls_back_only_to_dol_listed_periods(self) -> None:
        # DOL's page lists FY2025 Q4 and FY2026 Q2 only. FY2026 Q1 is a stray
        # file that still resolves on the legacy host but was never
        # published/linked, so the fallback must never even attempt it.
        self._discover_latest_period_mock.return_value = (2026, 2)
        server.data_manager.discovered_periods = [
            (2026, 2), (2025, 4), (2025, 3), (2025, 2), (2025, 1)
        ]

        attempted_periods: list[tuple[int | None, int | None]] = []

        def fake_load_data(self, year=None, quarter=None, force_download=False):
            attempted_periods.append((year, quarter))
            if (year, quarter) == (2025, 4):
                self.df = disclosure_rows()
                self.loaded_year, self.loaded_quarter = year, quarter
                self.source_url = "https://example.test/fake"
                return True
            # FY2026 Q2 (not downloadable yet) and the stray FY2026 Q1 file
            # (if wrongly attempted) both report failure here.
            return False

        with patch.object(server.H1BDataManager, "load_data", fake_load_data):
            result = server.data_manager.load_latest_data()

        self.assertTrue(result)
        self.assertEqual(server.data_manager.period_label(), "FY2025 Q4")
        self.assertNotIn((2026, 1), attempted_periods)

    def test_ask_latest_loads_discovered_period(self) -> None:
        self.cache_disclosure(2025, 4)
        self._discover_latest_period_mock.return_value = (2025, 4)

        result = server.ask("Load the latest H-1B data")

        self.assertEqual(result["action"], "load_h1b_data")
        self.assertEqual(result["result"]["data_version"], "FY2025 Q4")

    def test_search_applies_full_company_stats_to_each_matching_position(self) -> None:
        server.data_manager.df = pd.DataFrame(
            [
                {
                    "CASE_STATUS": "Certified",
                    "EMPLOYER_NAME": "Acme LLC",
                    "JOB_TITLE": "Software Engineer",
                    "WORKSITE_CITY": "Austin",
                    "WORKSITE_STATE": "TX",
                    "WAGE_RATE_OF_PAY_FROM": 150_000,
                },
                {
                    "CASE_STATUS": "Certified",
                    "EMPLOYER_NAME": "Acme LLC",
                    "JOB_TITLE": "Data Engineer",
                    "WORKSITE_CITY": "Dallas",
                    "WORKSITE_STATE": "TX",
                    "WAGE_RATE_OF_PAY_FROM": 170_000,
                },
                {
                    "CASE_STATUS": "Certified",
                    "EMPLOYER_NAME": "Acme LLC",
                    "JOB_TITLE": "Product Manager",
                    "WORKSITE_CITY": "Austin",
                    "WORKSITE_STATE": "TX",
                    "WAGE_RATE_OF_PAY_FROM": 160_000,
                },
            ]
        )

        result = server.search_h1b_jobs(
            "Engineer",
            max_results=10,
            skip_agencies=False,
        )

        self.assertEqual(result["total_matches"], 2)
        self.assertEqual(len(result["results"]), 2)
        stats = [position["company_stats"] for position in result["results"]]
        self.assertEqual(stats[0], stats[1])
        self.assertEqual(stats[0]["total_applications"], 3)
        self.assertEqual(stats[0]["certified"], 3)
        self.assertEqual(stats[0]["certification_rate"], 100.0)
        self.assertEqual(stats[0]["top_job_titles"]["Product Manager"], 1)

    def test_manual_and_automatic_company_checks_share_company_stats(self) -> None:
        server.data_manager.df = pd.DataFrame(
            [
                {
                    "CASE_STATUS": "Certified",
                    "EMPLOYER_NAME": "Google LLC",
                    "JOB_TITLE": "Software Engineer",
                    "WAGE_RATE_OF_PAY_FROM": 180_000,
                },
                {
                    "CASE_STATUS": "Denied",
                    "EMPLOYER_NAME": "Google LLC",
                    "JOB_TITLE": "Data Scientist",
                    "WAGE_RATE_OF_PAY_FROM": 190_000,
                },
            ]
        )

        manual = server.get_company_stats("Google")
        automatic = server.ask("Tell me about Google's H-1B statistics")

        self.assertEqual(automatic["action"], "get_company_stats")
        self.assertEqual(
            automatic["result"]["total_applications"],
            manual["total_applications"],
        )
        self.assertEqual(automatic["result"]["certified"], manual["certified"])
        self.assertEqual(automatic["result"]["top_job_titles"], manual["top_job_titles"])

    def test_discover_latest_period_parses_dol_performance_page(self) -> None:
        response = Mock()
        response.text = """
            LCA_Disclosure_Data_FY2025_Q4.xlsx
            LCA_Disclosure_Data_FY2026_Q1.xlsx
            LCA_Dislclosure_Data_FY2026_Q2.xlsx
        """

        with patch.object(server.requests, "get", return_value=response):
            period = server.H1BDataManager().discover_latest_period()

        self.assertEqual(period, (2026, 2))

    def test_company_stats_identifies_the_loaded_disclosure_period(self) -> None:
        self.cache_default_disclosure()

        result = server.get_company_stats("Google")

        self.assertEqual(result.get("fiscal_periods"), ["FY2024 Q4"])
        self.assertEqual(result.get("data_version"), "FY2024 Q4")
        self.assertEqual(
            result.get("source_url"),
            "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/"
            "LCA_Disclosure_Data_FY2024_Q4.xlsx",
        )

    def test_company_stats_counts_modern_title_case_certified_status(self) -> None:
        server.data_manager.df = disclosure_rows(case_status="Certified")

        result = server.get_company_stats("Google")

        self.assertEqual(result["certified"], 1)

    def test_job_search_accepts_modern_title_case_certified_status(self) -> None:
        server.data_manager.df = disclosure_rows(case_status="Certified")

        result = server.search_h1b_jobs("Software Engineer", max_results=1)

        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["returned"], 1)

    def test_requested_2025_quarter_is_not_replaced_with_q3(self) -> None:
        urls = server.H1BDataManager().get_dol_urls(2025, 4)

        self.assertTrue(urls[0].endswith("LCA_Disclosure_Data_FY2025_Q4.xlsx"))
        self.assertTrue(any("LCA_Dislclosure_Data_FY2025_Q4.xlsx" in url for url in urls))

    def test_available_data_reports_cached_periods_instead_of_calendar_guesses(self) -> None:
        self.cache_default_disclosure()
        server.get_company_stats("Google")

        result = server.get_available_data()

        self.assertEqual(result.get("loaded_period"), "FY2024 Q4")
        self.assertEqual(result.get("available_periods"), ["FY2024 Q4"])
        self.assertIsNone(result.get("latest_period"))
        self.assertNotIn("current_period", result)

    def test_http_health_is_reachable_immediately_and_ready_after_warmup(self) -> None:
        # The three-year cache warmup can take far longer than a platform
        # healthcheck timeout, so it must run in the background: /health has
        # to be reachable (even if "degraded") the instant the app starts,
        # not only after the warmup finishes.
        self.cache_default_disclosure()

        with TestClient(server.mcp.http_app()) as client:
            response = client.get("/health")
            self.assertEqual(response.status_code, 200)

            deadline = time.monotonic() + 5
            while (
                response.json()["status"] != "ok"
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
                response = client.get("/health")

        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(server.data_manager.is_loaded())

    def test_latest_startup_falls_back_when_newest_period_is_unavailable(self) -> None:
        manager = server.H1BDataManager()

        with patch.object(
            manager,
            "discover_latest_period",
            return_value=(2026, 2),
        ), patch.object(manager, "load_data", side_effect=[False, True]) as load_data:
            loaded = manager.load_latest_data()

        self.assertTrue(loaded)
        self.assertEqual(load_data.call_args_list[0].args, (2026, 2))
        self.assertEqual(load_data.call_args_list[1].args, (2026, 1))

    def test_http_startup_stays_live_when_data_is_unavailable(self) -> None:
        with TestClient(server.mcp.http_app()) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "degraded")
        self.assertFalse(server.data_manager.is_loaded())


if __name__ == "__main__":
    unittest.main()
