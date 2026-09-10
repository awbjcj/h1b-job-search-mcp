# Disk-backed H-1B service

## Diagnosis: 2026-09-10

The Railway project `121b62b6-b581-4478-9c92-54b3fa993059` has two services
in production (`94880fae-d0fa-4c7c-8bc5-9b781bc7f855`). The 24-hour resource
snapshot showed:

| Service | Average RAM (GB) | Current RAM (GB) | Peak RAM (GB) |
| --- | ---: | ---: | ---: |
| resume-tailor-harness | 0.3907 | 0.2749 | 0.9197 |
| h1b-job-search-mcp | 2.4838 | 2.4853 | 2.4858 |

H-1B accounts for about 86% of the combined average RAM. Its service ID is
`abc99b4c-63a1-46cf-9788-e1fe9a5b0d72`; its source repository is
`awbjcj/h1b-job-search-mcp`. The résumé app does not need a storage migration.
Both services already have volumes. The H-1B volume has a 5,000 MB capacity,
is mounted at `/app/data_cache`, and held six pickles totaling about 815 MiB
when inspected. The previous service retained a whole quarterly pandas frame.

Railway charges for actual average memory usage, so reducing a memory limit
alone does not reduce the memory the application needs. See
[Railway right-sizing](https://docs.railway.com/guides/right-size-cpu-memory)
and [volume documentation](https://docs.railway.com/volumes/reference).

## Runtime layout

- `server.py` owns period selection, DOL discovery/downloads and the MCP interface.
  The manager retains a database path, column names and row count.
- `disclosure_store.py` runs searches and aggregates in SQLite. Employer keys
  are normalized at import and indexed. Counts include every matching row;
  wages, medians, top titles and states are calculated in SQL. Company details
  are calculated only for employers in returned search rows.
- `import_disclosure.py` converts a legacy pickle or streams a new XLSX through
  `openpyxl` into SQLite in batches of 1,000 rows. Conversion runs in a separate
  process so pandas, workbook objects and allocator high-water memory disappear
  when conversion completes. The server imports neither pandas nor openpyxl.
- `employers.py` preserves the existing employer-name normalization rules.
- `container_runtime.py` repairs ownership of the mounted cache directories
  and drops to `appuser` before starting the server. Existing pickle contents
  do not need to be rewritten or recursively chowned.

Each SQLite query connection has an 8 MiB page-cache target, disabled memory
mapping, and file-backed temporary storage. Connections close after operations.
`SQLITE_TMPDIR` defaults to `data_cache/tmp` so SQL sorts use the mounted volume.
These settings bound dataset caching, not the entire Python process or all
SQLite allocations. There is one serving process; operations are serialized
around period selection. No extra database service or always-on worker is added.

`file_cache.py` retains clean operating-system file pages while a quarterly
database is in use, then releases them after **30 minutes without access**.
Each file has an independent monotonic idle timer, reset when a query finishes
(including error paths). Active readers prevent eviction. A background task
checks every 30 seconds even when no requests arrive, so normal cleanup occurs
30–30.5 minutes after the last reader finishes. Reader registration and eviction
share a lock to prevent a query from starting during cache release. The task
starts and stops with the server lifespan. Health checks do not reset the timer.

Imports release the committed database and the source file immediately after
their readers/writers close; subsequent queries follow the idle policy. On Linux this
uses `POSIX_FADV_DONTNEED` on only the relevant file; it does not delete data,
require root, or clear global caches. Unsupported platforms skip it, and OS
errors are logged without failing the operation. See the
[Linux file-advice documentation](https://man7.org/linux/man-pages/man2/posix_fadvise.2.html).

This preserves warm-cache performance during active sessions and lowers idle
RAM afterward. Frequent requests can keep a file cached indefinitely. SQLite and the kernel
can still allocate temporary memory during a query. The advice is best-effort,
not a hard memory ceiling; this is not a promise of 75 MB peak usage.

`H1B_DATA_CACHE_DIR` optionally overrides the cache directory. The existing
Railway mount works without adding another volume or increasing its capacity.
The container sets `MALLOC_ARENA_MAX=2` to limit glibc arena retention.

## Migration and compatibility

1. Prefer `LCA_<year>Q<quarter>.sqlite` when available.
2. Otherwise convert the existing trusted `.pkl` in a child process. Pickle
   cannot be streamed: the first conversion still needs enough RAM to read
   one legacy dataset. Keep the existing Railway memory ceiling during migration.
3. Build a unique `.building` file beside the destination, validate it, close
   the database and atomically replace the destination. Ordinary conversion
   failures clean the partial file and preserve the old database and pickle.
   A hard process/container kill can leave an incomplete `.building` artifact;
   it is never served. Remove such artifacts only after confirming no importer
   is running.
4. Retain legacy pickles for rollback. New downloads produce SQLite caches
   directly and discard the successfully imported XLSX. The existing six-quarter
   pruning policy recognizes both cache formats.

Failed loads preserve the previously selected valid store. Restart and quarter
switches reopen disk indexes without deserializing pandas frames. Existing tool
names and response shapes remain. Search/export limits and top-sponsor limits
now accept **0–1,000** to prevent unbounded response allocations; `total_matches`
still counts every matching row. Top-sponsor certified counts now recognize
both `Certified` and `CERTIFIED`, consistently with search/company statistics.

Keep sufficient free disk for the old pickles, new indexes and the largest
in-progress index. Monitor actual volume usage before deciding to remove rollback
files. Increasing a volume alone cannot eliminate the old resident DataFrame.

## Local evidence

Fresh Python processes on Windows used the same **100,000-row local cached
disclosure**. This local cache is smaller than the production FY2026 Q3 cache;
these measurements are not production RAM or billing guarantees.

| Measurement | Previous pandas implementation | SQLite implementation |
| --- | ---: | ---: |
| RAM after repeated queries | 301.0 MiB | 80.4 MiB |
| Peak process RAM | 346.6 MiB | 89.6 MiB |
| Load time | 0.474 s | 0.001 s |
| Example filtered search | 6.254 s | 0.197 s |
| Cache bytes | 70,828,871 | 104,095,744 |

The compared Google, Microsoft, Amazon and Woven by Toyota company responses
and a 10-result California software-engineer search matched exactly, including
embedded company statistics. The peak figures cover serving, not the separately
run one-time converter. This is about a 73% reduction in post-query RAM on this
fixture. Latencies are single-run measurements, not a load test.

Reproduce with `scripts/benchmark_memory.py` in separate processes. It refuses
network requests. Save the old `src/server.py` in a separate directory for the
baseline `--server-dir`; do not check out over local edits.

```powershell
.venv/Scripts/python.exe src/import_disclosure.py data_cache/LCA_2026Q3.pkl data_cache/LCA_2026Q3.sqlite
.venv/Scripts/python.exe scripts/benchmark_memory.py --cache-dir data_cache --year 2026 --quarter 3 --output data_cache/benchmark-after.json
.venv/Scripts/python.exe -m pytest -q
```

The 56-test suite covers API behavior, all-row aggregates, numeric/null wages,
odd/even medians, real XLSX streaming, migration in a real subprocess, fresh-process
reads without pandas/openpyxl, interrupted imports, concurrent reads, query cache
settings, result bounds, and container privilege-drop ordering. Python static
error checks pass. Cache-release tests also verify committed data, descriptor
cleanup, unsupported platforms and preservation of query/import failures.
Idle-expiry tests use an injected clock to cover the exact 1,800-second boundary,
timer resets, independent files, overlapping readers and cleanup without traffic.
A local Linux Docker build was not run because the Docker daemon
was unavailable.

## Production evidence and verification

### Current 30-minute idle policy

Revision `723490d` passed 56 local tests and deployed successfully as
`65f55c47-e2b1-460c-90fd-1232e2b5f694`. An isolated process running as Linux
UID 1000 verified the 1,800-second default, timer reset, active-reader protection,
and background release using an injected clock; it preserved the fixture bytes.
This was an accelerated test, not a real 30-minute wait on the production server.

Two rounds of actual production MCP calls (10 responses) matched baseline
fingerprints. Warm second-round timings were 0.182/0.186 seconds for company
lookups, 0.498 seconds for filtered search, 2.412 seconds for top sponsors, and
0.541 seconds for the six-quarter trend. Container memory including the probe
was approximately 921 MB with 830 MB of file cache, as intended during active
use. Pages remain eligible for cleanup after each file's 30-minute idle period.
These single-run timings are not a throughput benchmark.

### Earlier immediate-release measurements

The SQLite refactor reached `main` at `863a901`, deployed successfully as
`abb6429f-9e27-437c-ac64-40c39f9badc6`, and converted all six production caches.
The initial 73–85 MB Railway readings were fresh-start readings, not evidence
of memory after real traffic. Later inspection reproduced 741 MB of container
memory: approximately 667 MB file cache, 69 MB anonymous memory, and 5 MB kernel
memory. The app process RSS was about 91 MB. SQLite's 8 MiB cache setting does
not constrain the kernel cache that Railway reports.

A targeted advisory release reduced file cache from 667 MB to 0.6 MB without
a restart or any data changes. A baseline of real MCP company lookups, search,
top sponsors and a six-quarter trend then accumulated 828 MB of file cache
and 917 MB total (including the short-lived measurement process). This is why
automatic file-cache release is needed after an idle interval, not just import.

The measurements below describe the earlier **immediate-release policy**.
The current 30-minute idle policy intentionally retains file pages during
active use; do not expect its 74–97 MB post-query readings until idle eviction.

The cache-release fix `4f2ac11` passed all 52 local tests and reached Railway
`SUCCESS` in deployment `d0f50332-4231-443c-a51a-5525ed66db51`. Two production
rounds (10 calls) returned matching response fingerprints for Google/Microsoft
statistics, California software-engineer search, top sponsors and Google's
six-quarter trend. After the probe exited, container memory was 97.3 MB, with
68.5 MB anonymous memory and 24.4 MB filesystem cache. The two-round probe
finished at 112.3 MB including its own memory, with no second-round growth.
These are short-run observations, not billing guarantees.

| Operation | Baseline latency (seconds) | Cache-release latency, two rounds (seconds) |
| --- | ---: | ---: |
| Google company stats | 5.480 | 5.350 / 4.621 |
| Microsoft company stats | 1.214 | 6.590 / 6.594 |
| Filtered search, 10 results | 2.287 | 6.599 / 6.596 |
| Top 10 sponsors | 2.288 | 15.565 / 15.406 |
| Google six-quarter trend | 7.123 | 11.482 / 10.322 |

Baseline calls share the cache warmed by preceding calls; the new policy
releases each operation's pages. The largest sampled transient allocation was
577.2 MB including the probe, compared with 926.1 MB in the baseline. The active
query peak is materially higher than idle memory. The existing volume usage
remained about 2.0 GiB (44% of its filesystem capacity), with rollback pickles
preserved. No cache-release errors were recorded during these calls.

Use `scripts/profile_container_memory.py` **inside the running container**
after deployment to test the actual MCP HTTP endpoint. It runs two rounds of
those operations, records cgroup total/anonymous/file bytes, samples transient
memory, and prints SHA-256 response fingerprints without exposing job records.
The probe uses only the Python standard library. Compare fingerprints across
revisions and measure query latency as well as retained memory; its container
readings include the probe itself. After it exits, also read Railway metrics.
Do not treat a fresh-process RSS benchmark or a fresh-start metric as the
long-term container footprint.

Deploy only the H-1B service from the reviewed revision. Preserve its volume,
domains, replica count and memory ceiling. Wait for Railway `SUCCESS`, verify
`/health`, all six indexed quarters, queries, free disk and logs, then collect
a longer usage window before claiming monthly savings. A code rollback can
reuse retained legacy pickles; do not delete the volume or existing datasets.
