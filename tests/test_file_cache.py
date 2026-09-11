"""Cache eviction is optional and must preserve data and query errors."""

import asyncio
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


def test_query_failure_schedules_release_after_connection_closes(tmp_path, monkeypatch):
    path = tmp_path / "quarter.sqlite"
    write_database(path, ["EMPLOYER_NAME"], [("Acme",)])
    cache = file_cache.IdleFileCache(idle_seconds=0)
    monkeypatch.setattr("disclosure_store.query_file_cache", cache)
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
    assert descriptors == []
    assert cache.evict_idle() == 1
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    monkeypatch.delattr(file_cache.os, "posix_fadvise")
    stats = store.company_stats("Acme", None, None)
    assert stats is not None
    assert stats["total_applications"] == 1


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


def test_idle_timeout_resets_on_each_access(tmp_path, monkeypatch):
    now, released = [0.0], []
    cache = file_cache.IdleFileCache(clock=lambda: now[0])
    path = tmp_path / "quarter.sqlite"
    monkeypatch.setattr(file_cache, "release_file_cache", released.append)
    with cache.use(path):
        pass
    now[0] = 1799
    assert cache.evict_idle() == 0
    with cache.use(path):
        pass
    now[0] = 1800
    assert cache.evict_idle() == 0
    now[0] = 3598
    assert cache.evict_idle() == 0
    now[0] = 3599
    assert cache.evict_idle() == 1
    assert released == [str(path.resolve())]
    assert cache.evict_idle() == 0


def test_quarters_expire_independently(tmp_path, monkeypatch):
    now, released = [0.0], []
    cache = file_cache.IdleFileCache(clock=lambda: now[0])
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setattr(file_cache, "release_file_cache", released.append)
    with cache.use(first):
        pass
    now[0] = 1000
    with cache.use(second):
        pass
    now[0] = 1800
    assert cache.evict_idle() == 1
    assert released == [str(first.resolve())]
    now[0] = 2800
    assert cache.evict_idle() == 1
    assert released == [str(first.resolve()), str(second.resolve())]


def test_active_readers_are_never_evicted_and_timeout_starts_after_last_reader(
    tmp_path, monkeypatch
):
    now, released = [0.0], []
    cache = file_cache.IdleFileCache(clock=lambda: now[0])
    monkeypatch.setattr(file_cache, "release_file_cache", released.append)
    path = tmp_path / "quarter"
    with cache.use(path):
        with cache.use(path):
            now[0] = 3600
            assert cache.evict_idle() == 0
        now[0] = 7200
        assert cache.evict_idle() == 0
    now[0] = 8999
    assert cache.evict_idle() == 0
    now[0] = 9000
    assert cache.evict_idle() == 1


def test_background_cleanup_runs_without_new_queries(tmp_path, monkeypatch):
    async def scenario():
        cache = file_cache.IdleFileCache(idle_seconds=0)
        evicted = asyncio.Event()
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            file_cache,
            "release_file_cache",
            lambda path: loop.call_soon_threadsafe(evicted.set),
        )
        with cache.use(tmp_path / "quarter"):
            pass
        task = asyncio.create_task(cache.run(interval=0.001))
        try:
            await asyncio.wait_for(evicted.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cache.evict_idle() == 0

    asyncio.run(scenario())
