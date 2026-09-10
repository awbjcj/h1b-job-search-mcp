"""Release disposable Linux file pages without changing persistent data."""

import asyncio
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

IDLE_CACHE_SECONDS = 30 * 60
CACHE_SWEEP_SECONDS = 30


class IdleFileCache:
    """Retain hot files, releasing each after 30 minutes without a reader."""

    def __init__(self, idle_seconds=IDLE_CACHE_SECONDS, clock=time.monotonic):
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._files = {}

    @contextmanager
    def use(self, path):
        path = str(Path(path).resolve())
        with self._lock:
            active, _ = self._files.get(path, (0, 0))
            self._files[path] = (active + 1, self.clock())
        try:
            yield
        finally:
            with self._lock:
                active, _ = self._files[path]
                self._files[path] = (active - 1, self.clock())

    def evict_idle(self):
        # Keep the lock through eviction: a new reader cannot start between
        # the idle check and the cache release. Active readers are skipped.
        with self._lock:
            now = self.clock()
            expired = [
                path
                for path, (active, touched) in self._files.items()
                if active == 0 and now - touched >= self.idle_seconds
            ]
            for path in expired:
                release_file_cache(path)
                del self._files[path]
            return len(expired)

    async def run(self, interval=CACHE_SWEEP_SECONDS):
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(self.evict_idle)


query_file_cache = IdleFileCache()


def release_file_cache(path):
    """Best-effort eviction after the reader/writer has closed the file.

    SQLite's page-cache limit does not cover Linux's filesystem cache, which
    Railway includes in container memory. DONTNEED discards only clean cached
    pages; committed database contents remain on the volume. No global cache
    controls or elevated privileges are needed. Unsupported platforms skip it.
    """
    advise = getattr(os, "posix_fadvise", None)
    advice = getattr(os, "POSIX_FADV_DONTNEED", None)
    if advise is None or advice is None:
        return
    try:
        with open(path, "rb", buffering=0) as file:
            advise(file.fileno(), 0, 0, advice)
    except OSError as error:
        # Cache policy must not fail a query or mask its original exception.
        logger.warning("Cannot release file cache for %s: %s", path, error)
