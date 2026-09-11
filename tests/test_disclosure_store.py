"""Persistence, migration, and aggregate regression tests using real disk files."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from disclosure_store import DisclosureStore
from import_disclosure import convert, write_database

COLUMNS = [
    "EMPLOYER_NAME",
    "JOB_TITLE",
    "CASE_STATUS",
    "WAGE_RATE_OF_PAY_FROM",
    "WORKSITE_STATE",
    "WORKSITE_CITY",
]


def build(path, rows):
    write_database(path, COLUMNS, iter(rows))
    return DisclosureStore(str(path))


def test_aggregates_cover_all_rows_and_numeric_wages(tmp_path):
    rows = [
        ("The Acme, L.L.C.", "Engineer", "Certified", "100", "TX", "Austin"),
        (
            "ACME LIMITED LIABILITY COMPANY",
            "Engineer",
            "CERTIFIED",
            200,
            "TX",
            "Dallas",
        ),
        ("Acme LLC", "Manager", "Denied", "bad", "CA", None),
        ("Acme LLC", None, "Withdrawn", None, None, None),
    ]
    store = build(tmp_path / "quarter.sqlite", rows)
    stats = store.company_stats("Acme", "FY2026 Q3", "source")
    assert stats is not None
    assert stats["total_applications"] == 4
    assert stats["certified"] == 2
    assert stats["denied"] == 1
    assert stats["certification_rate"] == 50
    assert stats["wage_stats"] == {"min": 100, "max": 200, "mean": 150, "median": 150}
    assert stats["top_job_titles"] == {"Engineer": 2, "Manager": 1}
    result = store.search(
        "Engineer", "Austin", "TX", 90, 1, False, "FY2026 Q3", "source"
    )
    assert result["total_matches"] == 1
    assert result["results"][0]["company_stats"] == stats
    assert store.company_stats("Acme' OR 1=1 --", None, None) is None


def test_odd_median_empty_wages_and_dba_column(tmp_path):
    target = tmp_path / "quarter.sqlite"
    write_database(
        target,
        ["EMPLOYER_BUSINESS_DBA", "PREVAILING_WAGE"],
        iter([("Example", 5), ("Example", 9), ("Example", 7), ("Empty", "bad")]),
    )
    store = DisclosureStore(str(target))
    stats = store.company_stats("Example", None, None)
    assert stats is not None
    assert stats["wage_stats"]["median"] == 7
    assert stats["certified"] == "N/A"
    empty_stats = store.company_stats("Empty", None, None)
    assert empty_stats is not None
    assert empty_stats["wage_stats"] == dict.fromkeys(
        ("min", "max", "mean", "median")
    )
    assert (
        store.search("unused", None, None, None, 2, True, None, None)["total_matches"]
        == 4
    )


def test_atomic_replacement_preserves_old_database_on_import_failure(tmp_path):
    target = tmp_path / "quarter.sqlite"
    build(target, [("Original", "Engineer", "Certified", 100, "CA", "LA")])

    def interrupted():
        for _ in range(1500):
            yield ("Replacement", "Engineer", "Certified", 200, "CA", "LA")
        raise RuntimeError("simulated importer crash")

    with pytest.raises(RuntimeError, match="importer crash"):
        write_database(target, COLUMNS, interrupted())
    stats = DisclosureStore(str(target)).company_stats("Original", None, None)
    assert stats is not None
    assert stats["total_applications"] == 1
    assert list(tmp_path.iterdir()) == [target]


def test_streamed_workbook_keeps_all_batches(tmp_path):
    source, target = tmp_path / "input.xlsx", tmp_path / "quarter.sqlite"
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet()
    sheet.append(COLUMNS)
    for i in range(2501):
        sheet.append(("Acme", "Engineer", "Certified", i, "CA", "LA"))
    workbook.save(source)
    workbook.close()
    assert convert(source, target) == 2501
    stats = DisclosureStore(str(target)).company_stats("Acme", None, None)
    assert stats is not None
    assert stats["total_applications"] == 2501
    assert stats["wage_stats"]["median"] == 1250


def test_real_child_migrates_pickle_and_clean_process_reads_without_pandas(tmp_path):
    source, target = tmp_path / "legacy.pkl", tmp_path / "quarter.sqlite"
    pd.DataFrame(
        [("Acme", "Engineer", "Certified", 100, "CA", "LA")], columns=COLUMNS
    ).to_pickle(source)
    src = Path(__file__).resolve().parents[1] / "src"
    subprocess.run(
        [sys.executable, str(src / "import_disclosure.py"), str(source), str(target)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert source.exists()
    code = (
        "import json,sys; from disclosure_store import DisclosureStore; "
        "store=DisclosureStore(sys.argv[1]); "
        'print(json.dumps(store.company_stats("Acme", None, None))); '
        'assert "pandas" not in sys.modules; assert "openpyxl" not in sys.modules'
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(target)],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
        env={**os.environ, "PYTHONPATH": str(src)},
    )
    assert json.loads(result.stdout)["total_applications"] == 1


def test_result_bounds_and_invalid_regex(tmp_path):
    store = build(
        tmp_path / "quarter.sqlite", [("Acme", "Engineer", "Certified", 1, "CA", "LA")]
    )
    for limit in (-1, 1001):
        assert "error" in store.search("", None, None, None, limit, False, None, None)
        assert "error" in store.top_sponsors(limit, False)
    assert "error" in store.search("[", None, None, None, 1, False, None, None)
    empty = store.search("", None, None, None, 0, False, None, None)
    assert empty["total_matches"] == 1
    assert empty["returned"] == 0


def test_search_does_not_aggregate_employers_beyond_returned_limit(
    tmp_path, monkeypatch
):
    store = build(
        tmp_path / "quarter.sqlite",
        [(f"Company {i}", "Engineer", "Certified", 100, "CA", "LA") for i in range(30)],
    )
    real_stats = store._stats
    calls = []

    def counted(*args):
        calls.append(args[2])
        return real_stats(*args)

    monkeypatch.setattr(store, "_stats", counted)
    result = store.search("Engineer", None, None, None, 2, False, None, None)
    assert result["total_matches"] == 30
    assert result["returned"] == 2
    assert len(calls) == 2


def test_nullable_legacy_columns_are_stored_as_null(tmp_path):
    source, target = tmp_path / "legacy.pkl", tmp_path / "quarter.sqlite"
    pd.DataFrame(
        {
            "EMPLOYER_NAME": ["Acme", "Acme"],
            "WAGE_RATE_OF_PAY_FROM": pd.array([100, pd.NA], dtype="Int64"),
            "DECISION_DATE": [pd.Timestamp("2026-01-01"), pd.NaT],
        }
    ).to_pickle(source)
    assert convert(source, target) == 2
    store = DisclosureStore(str(target))
    stats = store.company_stats("Acme", None, None)
    assert stats is not None
    assert stats["wage_stats"]["mean"] == 100


def test_top_sponsors_filters_agencies_and_casefolds_status(tmp_path):
    store = build(
        tmp_path / "quarter.sqlite",
        [
            ("Acme", "Engineer", "Certified", 100, "TX", "Austin"),
            ("Acme", "Engineer", "CERTIFIED", 200, "CA", "LA"),
            ("Agency staffing", "Engineer", "Certified", 300, "WA", "Seattle"),
        ],
    )
    result = store.top_sponsors(10, True)
    assert result == {
        "total_companies": 1,
        "top_sponsors": [
            {
                "company": "Acme",
                "total_applications": 2,
                "certified": 2,
                "avg_wage": 150,
                "primary_state": "CA",
            }
        ],
    }


def test_query_connections_release_caches_and_use_disk_for_sorting(tmp_path):
    store = build(
        tmp_path / "quarter.sqlite", [("Acme", "Engineer", "Certified", 1, "CA", "LA")]
    )
    with store.connect() as db:
        assert db.execute("PRAGMA cache_size").fetchone()[0] == -8192
        assert db.execute("PRAGMA temp_store").fetchone()[0] == 1
        assert db.execute("PRAGMA mmap_size").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM disclosures")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
