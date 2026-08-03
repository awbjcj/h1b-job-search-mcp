import sys
import tempfile
import unittest
import warnings
from pathlib import Path

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

    def tearDown(self) -> None:
        server.DATA_CACHE_DIR = self._original_cache_dir
        server.data_manager = self._original_data_manager
        self._temp_dir.cleanup()

    def cache_default_disclosure(self, data: pd.DataFrame | None = None) -> None:
        disclosure = data if data is not None else disclosure_rows()
        disclosure.to_pickle(Path(self._temp_dir.name) / "LCA_2024Q4.pkl")

    def test_company_stats_loads_default_cache_on_first_read(self) -> None:
        self.cache_default_disclosure()

        result = server.get_company_stats("Google")

        self.assertNotIn("error", result)
        self.assertEqual(result["total_applications"], 1)

    def test_loaded_data_accessor_rejects_unloaded_manager(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "data is not loaded"):
            server.data_manager.get_loaded_data()

    def test_load_h1b_data_reports_cached_frame_details(self) -> None:
        self.cache_default_disclosure()

        result = server.load_h1b_data()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["records_loaded"], 1)
        self.assertIn("CASE_STATUS", result["columns"])

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

    def test_available_data_reports_cached_periods_instead_of_calendar_guesses(self) -> None:
        self.cache_default_disclosure()
        server.get_company_stats("Google")

        result = server.get_available_data()

        self.assertEqual(result.get("loaded_period"), "FY2024 Q4")
        self.assertEqual(result.get("available_periods"), ["FY2024 Q4"])
        self.assertNotIn("current_period", result)

    def test_http_startup_loads_data_before_health_reports_ready(self) -> None:
        self.cache_default_disclosure()

        with TestClient(server.mcp.http_app()) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(server.data_manager.is_loaded())


if __name__ == "__main__":
    unittest.main()
