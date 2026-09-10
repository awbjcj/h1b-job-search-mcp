"""Run inside the service container to measure memory after real MCP requests.

Uses only the standard library. Prints response fingerprints, never job records.
The cgroup reading includes the short-lived probe as well as the serving process.
"""

import argparse
import hashlib
import json
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen


def memory():
    root = Path("/sys/fs/cgroup")
    stats = dict(
        line.split() for line in (root / "memory.stat").read_text().splitlines()
    )
    return {
        "container_bytes": int((root / "memory.current").read_text()),
        "anon_bytes": int(stats["anon"]),
        "file_bytes": int(stats["file"]),
    }


def call(url, name, arguments):
    request = Request(
        url,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        ).encode(),
        {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urlopen(request, timeout=120) as response:
        raw = response.read().decode()
    if raw.lstrip().startswith("{"):
        message = json.loads(raw)
    else:
        message = next(
            json.loads(line[6:])
            for line in raw.splitlines()
            if line.startswith("data: ") and '"result"' in line
        )
    if "error" in message:
        raise RuntimeError(message["error"])
    result = message["result"]
    if result.get("isError"):
        raise RuntimeError(result)
    payload = result.get("structuredContent")
    if payload is None:
        payload = json.loads(result["content"][0]["text"])
    if "error" in payload:
        raise RuntimeError(payload["error"])
    if name == "get_company_sponsorship_trend":
        assert payload["period_count"] == 6 and not payload["missing_periods"]
    elif name == "search_h1b_jobs":
        assert payload["returned"] == 10 and payload["total_matches"] >= 10
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/mcp")
    parser.add_argument("--rounds", type=int, default=2)
    args = parser.parse_args()
    cases = [
        ("get_company_stats", {"company_name": "Google"}),
        ("get_company_stats", {"company_name": "Microsoft"}),
        (
            "search_h1b_jobs",
            {"job_role": "Software Engineer", "state": "CA", "max_results": 10},
        ),
        ("get_top_sponsors", {"limit": 10}),
        ("get_company_sponsorship_trend", {"company_name": "Google"}),
    ]
    print(json.dumps({"stage": "before", **memory()}), flush=True)
    for round_number in range(args.rounds):
        for name, arguments in cases:
            samples = [memory()["container_bytes"]]
            stop = threading.Event()

            def sample():
                while not stop.wait(0.05):
                    samples.append(memory()["container_bytes"])

            sampler = threading.Thread(target=sample, daemon=True)
            sampler.start()
            started = time.perf_counter()
            try:
                digest = call(args.url, name, arguments)
            finally:
                stop.set()
                sampler.join()
            seconds = time.perf_counter() - started
            # Let deferred kernel accounting settle after the response.
            time.sleep(0.5)
            print(
                json.dumps(
                    {
                        "round": round_number + 1,
                        "tool": name,
                        "arguments": arguments,
                        "sha256": digest,
                        "seconds": round(seconds, 3),
                        "sampled_peak_bytes": max(samples),
                        **memory(),
                    }
                ),
                flush=True,
            )
    time.sleep(3)
    print(json.dumps({"stage": "idle", **memory()}), flush=True)


if __name__ == "__main__":
    main()
