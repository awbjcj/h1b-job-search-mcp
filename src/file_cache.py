"""Release disposable Linux file pages without changing persistent data."""

import logging
import os

logger = logging.getLogger(__name__)


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
