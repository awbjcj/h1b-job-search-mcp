"""Offline, fresh-process benchmark against an existing quarterly cache.

Pass --server-dir pointing at a baseline src directory to compare revisions.
No downloads or Railway credentials are used. Run conversion separately so its
one-time peak is not confused with steady serving memory.
"""

import argparse
import ctypes
import json
import os
import sys
import time
from pathlib import Path


def memory_mib():
    if os.name == "nt":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                )
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        get_process = ctypes.windll.kernel32.GetCurrentProcess
        get_process.restype = wintypes.HANDLE
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        if not get_memory(get_process(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return {
            "rss_mib": counters.WorkingSetSize / 2**20,
            "peak_rss_mib": counters.PeakWorkingSetSize / 2**20,
        }
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf(
        "SC_PAGE_SIZE"
    )
    return {"rss_mib": rss / 2**20, "peak_rss_mib": peak / 1024}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server-dir", type=Path, default=Path(__file__).resolve().parents[1] / "src"
    )
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--quarter", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.server_dir.resolve()))
    import server

    server.DATA_CACHE_DIR = str(args.cache_dir.resolve())
    server.data_manager = server.H1BDataManager()
    server.data_manager.discover_latest_period = lambda: (args.year, args.quarter)
    server.data_manager.latest_available_year = args.year
    server.data_manager.latest_available_quarter = args.quarter
    server.requests.get = lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("Offline benchmark")
    )
    measurements = {"import": memory_mib()}
    start = time.perf_counter()
    assert server.data_manager.load_data(args.year, args.quarter)
    measurements["load"] = {**memory_mib(), "seconds": time.perf_counter() - start}
    results = {}
    for company in ("Google", "Microsoft", "Amazon", "Woven by Toyota, U.S., Inc."):
        start = time.perf_counter()
        results[company] = server.get_company_stats(company)
        measurements[company] = {**memory_mib(), "seconds": time.perf_counter() - start}
    start = time.perf_counter()
    results["search"] = server.search_h1b_jobs(
        "Software Engineer", state="CA", max_results=10
    )
    measurements["search"] = {**memory_mib(), "seconds": time.perf_counter() - start}
    # A second pass also detects a query cache that grows on repeated reads.
    for _ in range(3):
        server.get_company_stats("Google")
    measurements["repeat"] = memory_mib()
    payload = {"measurements": measurements, "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, default=lambda v: v.item(), indent=2), encoding="utf-8"
    )
    print(json.dumps(measurements, indent=2))


if __name__ == "__main__":
    main()
