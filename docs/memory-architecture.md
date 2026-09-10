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

The 47-test suite covers API behavior, all-row aggregates, numeric/null wages,
odd/even medians, real XLSX streaming, migration in a real subprocess, fresh-process
reads without pandas/openpyxl, interrupted imports, concurrent reads, query cache
settings, result bounds, and container privilege-drop ordering. Python static
checks pass. A local Linux Docker build was not run because the Docker daemon
was unavailable.

## Production rollout status and verification

The refactor is local on `codex/volume-backed-h1b`; it is not committed, pushed
or deployed. The running H-1B revision remains `2d5db2d`, deployment
`428a9d65-3a39-4131-aa32-9f29e17afb5b`. No production cost reduction has been
verified yet.

After production deployment is authorized, deploy only this H-1B service from
the reviewed revision. Preserve its current volume, domains, replica count and
memory ceiling. Wait for Railway `SUCCESS`, then verify `/health`, all six
indexed quarters, company lookup, search, trends, free disk and logs. Compare
idle and query RAM after the importer exits, then collect a longer usage window
before claiming monthly savings. A code rollback can reuse retained legacy
pickles; do not delete the volume or existing datasets.
