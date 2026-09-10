"""Cache eviction is optional and must preserve data and query errors."""

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import file_cache
from disclosure_store import DisclosureStore
from import_disclosure import convert, write_database


def test_eviction_keeps_file_contents_and_closes_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "data"
    path.write_bytes(b"persistent disclosure data")
    descriptors = []

    def advise(fd, offset, length, advice):
        assert os.fstat(fd).st_size == 26
        assert (offset, length, advice) == (0, 0, 4)
        descriptors.append(fd)

    monkeypatch.setattr(file_cache.os, "posix_fadvise", advise, raising=False)
    monkeypatch.setattr(file_cache.os, "POSIX_FADV_DONTNEED", 4, raising=False)
    file_cache.release_file_cache(path)
    assert path.read_bytes() == b"persistent disclosure data"
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_unsupported_platform_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delattr(file_cache.os, "posix_fadvise", raising=False)
    file_cache.release_file_cache(tmp_path / "missing")


def test_query_failure_still_releases_pages_after_connection_closes(
    tmp_path, monkeypatch
):
    path = tmp_path / "quarter.sqlite"
    write_database(path, ["EMPLOYER_NAME"], [("Acme",)])
    store = DisclosureStore(str(path))
    descriptors = []

    def advise(fd, *_args):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")
        descriptors.append(fd)
        raise OSError("unsupported filesystem")

    monkeypatch.setattr(file_cache.os, "posix_fadvise", advise, raising=False)
    monkeypatch.setattr(file_cache.os, "POSIX_FADV_DONTNEED", 4, raising=False)
    with pytest.raises(ValueError, match="original query failure"):
        with store.connect() as db:
            raise ValueError("original query failure")
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    monkeypatch.delattr(file_cache.os, "posix_fadvise")
    assert store.company_stats("Acme", None, None)["total_applications"] == 1


def test_import_releases_committed_database_and_source(tmp_path, monkeypatch):
    import pandas as pd

    source, target = tmp_path / "source.pkl", tmp_path / "quarter.sqlite"
    pd.DataFrame({"EMPLOYER_NAME": ["Acme"]}).to_pickle(source)
    before = source.read_bytes()
    released = []

    def release(path):
        released.append(Path(path))
        if str(path).endswith(".building"):
            # A separate connection sees the committed contents before release.
            with closing(sqlite3.connect(path)) as db:
                assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                assert db.execute("SELECT row_count FROM metadata").fetchone()[0] == 1

    monkeypatch.setattr("import_disclosure.release_file_cache", release)
    assert convert(source, target) == 1
    assert released[0].suffix == ".building"
    assert released[1] == source
    assert source.read_bytes() == before
    assert len(DisclosureStore(str(target))) == 1


def test_failed_conversion_also_releases_source(tmp_path, monkeypatch):
    source = tmp_path / "broken.pkl"
    source.write_bytes(b"invalid pickle")
    released = []
    monkeypatch.setattr("import_disclosure.release_file_cache", released.append)
    with pytest.raises(Exception):
        convert(source, tmp_path / "quarter.sqlite")
    assert released == [source]
