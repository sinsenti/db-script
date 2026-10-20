# Shared Python / Go / Rust comparison contract

The Python implementation is the reference. This directory is language-neutral and is shared byte-for-byte between the Go and Rust worktrees. Do not fork the fixture, operations, normalization, or timing methodology; propose corrections to the Go owner and sync the complete directory.

## Fixture and reset rules

`fixture.py` creates `small` (128 people) and `large` (10,000 people) SQLite databases using seed `20250308`. It writes a SHA-256 manifest. Before *each* candidate and each mutating workload, copy the fixture file to a fresh disposable database. Never benchmark candidate runs on a previously mutated file. Fixture tables include indexes, FK relations, composite and absent primary keys, NULL, Unicode, apostrophes, blobs, JSON values and stable tie-breaking keys. The sorted workloads use unique `people.id` or `orders.order_id` values; no unspecified equal-key row order is asserted.

## Machine runner protocol

A candidate process is started once per database as:

```
<candidate> --protocol --db <copied-fixture> [--read-only]
```

Python reference: `python3 benchmarks/language_comparison/python_runner.py --db <copied-fixture> [--read-only]`.

The process reads one UTF-8 JSON object per stdin line and writes exactly one JSON object per request to stdout. Diagnostics go to stderr. Supported request objects:

- `{"op":"list_tables","include_views":false}`
- `{"op":"schema","table":"people"}`
- `{"op":"foreign_keys","table":"orders"}`
- `{"op":"page","table":"people","limit":8,"offset":0,"filter":"Ada","sort":["id","ASC"]}` (filter/sort optional)
- `{"op":"count","table":"people","filter":"Ada"}`
- `{"op":"insert","table":"people","row":{"id":...}}`
- `{"op":"update","table":"people","original":{"id":...},"updated":{"id":...}}`
- `{"op":"delete","table":"people","original":{"id":...}}`
- `{"op":"raw","sql":"SELECT ...","params":[...],"limit":20,"offset":0}`
- `{"op":"render","table":"orders","limit":6,"offset":0,"filter":"order_id >= 1","sort":["order_id","ASC"],"width":100,"height":24}`

Success is `{"ok":true,"result":...,"trace":[{"sql":"...","params":[...]}]}`. Failure is `{"ok":false,"error":{"class":"...","message":"..."},"trace":[...]}`. Every result includes an empty trace when no SQL ran. Values are JSON primitives; SQLite BLOB values are `{"$blob":"<RFC 4648 base64>"}`. NULL remains JSON null. SQL strings and separately bound parameter arrays are compared exactly, along with order and values; trace entries represent the actual production adapter `execute(sql, params)` calls, not a second SQL builder.

Result shapes: page/raw `{headers:[...],rows:[[...],...]}`; schema same; foreign keys is an ordered array of `{from_col,to_table,to_col}`; tables is an ordered array; count and mutations are integers; render is `{screen:"..."}`. Error class/message and SQL trace are compared; only JSON object key order is ignored. Do not normalize row order, types, NULLs, blobs, errors, SQL or bind values.

The `render` request must call production adapter paging/count and the candidate's production table-rendering function for the same fixed dimensions/content. It returns the full bounded data-browser screen (status, table, and navigation/help lines), not an adapter-only or minimal table dump. The shared Python runner calls `dbcli.ui.get_fitted_columns` and `dbcli.ui.build_ascii_table_lines`.

## Shared validation workload

`workloads.json` is authoritative for the same request sequence and SQL on all candidates. Successful mutation sequence is run against a fresh identical copy: insert, update a row using its *original* key while changing that key, verify new state, update with the composite key, delete using the composite key, and delete the inserted row. Rejections are run on separate fresh copies, including invalid sort/filter columns, invalid pages, missing/composite identity, no-primary-key mutation, read-only writes, raw writes, and a raw side-effect rejection. A candidate must pass parity before timing is allowed.

## Timing protocol

Use `benchmark.py` only after correctness passes. Build optimized/release binaries first and record build time separately. Benchmark Python, Go, Rust together after both agents announce readiness; acquire the shared exclusive timing lock (`/tmp/db-script-language-comparison.benchmark.lock`, `flock`) so agents never time concurrently (the lock must be outside either worktree so both branches share it). Candidate order is shuffled/interleaved per round. Report distributions and preserve raw JSON/CSV. Separate fresh-process startup + first query, fresh-process first-touch DB (not OS-cache-cold), and repeated warm-process operations. Do not claim OS cache eviction. Record wall/user/system CPU, peak RSS (platform units normalized to bytes), artifact size, Python runtime/dependency context, versions, fixture hash, OS/CPU, SQLite version/settings, warmups/sample count and instrumentation overhead. The JSONL IPC and trace collection are instrumentation overhead and must not be conflated with direct UI/database timings.

Benchmark database operations use the exact requests in `workloads.json`; CRUD is never run repeatedly against an evolving database. Report build/setup effort and source LOC/dependency footprint as observations, not a universal language ranking. PostgreSQL/MySQL selected live integration tests use only the repository's disposable Compose services and explicit localhost fixture URLs (ports 55432 and 53306); report unavailable services/drivers as skips/blockers, never passes.
