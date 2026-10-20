# Repeatable PostgreSQL and Neovim benchmarks

Adapted from `codex_codex_quotes_perf` (`a44b69c`). All default executable,
reference-adapter and helper paths now resolve inside this checkout. No sibling
worktree, previous virtual environment, stored benchmark result or fixed container
ID is required. These are development tools; application runtime imports none of them.

## Setup

Run from the repository root on Linux (POSIX PTYs and Linux `/proc` are used):

```bash
bash tools/build_rust.sh
python3 -m venv .local/performance-venv
.local/performance-venv/bin/pip install -r benchmarks/quotes_perf/requirements.txt
```

Neovim must be on PATH for UI timings. A clean profile is used for reproducibility;
`--nvim /absolute/path/to/nvim` selects another executable.

## Run against your quotes table

```bash
# Rust and the local reference Python adapter, after matching every workload:
.local/performance-venv/bin/python benchmarks/quotes_perf/backend_bench.py \
  --container a1labs-postgres-1 --table public.quotes --repetitions 9

# Ratatui and original Rust terminal UI; actual Neovim sessions always cancel:
.local/performance-venv/bin/python benchmarks/quotes_perf/ui_bench.py \
  --container a1labs-postgres-1 --table public.quotes \
  --interfaces modern legacy --repetitions 3

# Print Markdown tables:
.local/performance-venv/bin/python benchmarks/quotes_perf/summarize.py \
  .local/performance/backend-results.json .local/performance/ui-results.json
```

Reports default to ignored `.local/performance/` and are written atomically with
mode 0600. Existing reports require `--overwrite` or a new `--output PATH`. Keep
separate output files when comparing builds. Docker passwords are resolved in
memory. For an already configured `DBCLI_POSTGRES_URL`, use `--from-env` instead
of `--container`; never put credentials in arguments. Local loopback PostgreSQL
with `sslmode=disable` is required by this benchmark's packet guard.

Targets are explicit: `--table` accepts a schema-qualified identifier, including
quoted components. Page workloads currently require a single-column primary key
for deterministic ordering. The UI benchmark requires at least one row. Backend
filtering defaults to `PK IS NOT NULL`; `--filter 'status IS NOT NULL'` chooses a
trusted SQL predicate for a more selective workload. The same predicate and sort
are used by the oracle and every candidate. Arbitrary untrusted predicates are
outside this tool's scope.

## What is measured

- **Validation first:** table discovery, schema, first/next/descending/filtered
  pages, count, identical supplied raw SQL and bounded bulk reads must all match
  a PostgreSQL oracle before any candidate is timed. Timed responses and final
  oracle stability are checked too. SQL NULL, JSON null, exact decimals, UUIDs,
  bytea and common typed arrays are compared using catalog types.
- **Backend:** randomized repeated warm reads, per-call worker CPU, worker RSS
  and lifetime peak RSS; repeated fresh process + `SELECT 1` startup. Python uses
  the local reference adapter with a server-read-only connection factory, not the
  optional Textual frontend. Table discovery results are restricted to the target
  schema to match Python; native discovery still scans all non-system schemas.
  `same_sql_page` is the controlled supplied-SQL comparison. Backend serialization,
  metadata work and SQL wrappers can otherwise differ.
- **UI:** startup to a completed stable frame, `E` to editor launch and actual
  Neovim readiness, in-Neovim unsaved diff rendering, whole-process-tree sampled
  CPU/RSS and exact terminal/alternate-screen restoration. The all-thread process
  collector runs automatically every 20ms; a second terminal is unnecessary.
- **Distribution:** executable sizes and SHA-256, source/lockfile/harness hashes;
  artifacts must remain unchanged throughout a run. Python/package, PostgreSQL
  server and Neovim versions are recorded too. App executable sizes exclude shared libraries,
  interpreter dependencies and external Neovim; Neovim is fingerprinted separately. An overridden worker/frontend's
  executable hash is recorded; its source tree is not inferred or attested.

Bulk reads retain the 100,000-row cap. CPU excludes PostgreSQL server work. Linux
CPU tick resolution and the 20ms sampler can miss short-lived work and peaks;
readiness sampling lag and Neovim presence are reported. Summaries exclude readiness
CPU/RSS samples arriving more than 100ms late or without Neovim present. Summed RSS can count
shared mappings more than once. CPU totals retain the last observation of exited
processes. Peak RSS is a sampled tree peak, not a kernel-provided tree high-water
mark. UI startup includes 150ms frame stability. Run builds/tests first, then
backend and UI benchmarks sequentially on an otherwise quiet machine.

Fresh-process measurements use a warm server and OS caches. No cold-server or
language-isolation claim is made. The tools never restart a database or clear caches.
No development-productivity or MySQL benchmark is included.

## Read-only benchmark behavior

Backend and oracle reads use bounded server-enforced read-only transactions.
UI sessions keep `E` available but pass through the same local PostgreSQL packet
guard. It rejects generated CRUD/DDL before forwarding. Neovim modifies one line
of JSON whitespace in memory to exercise the diff, never saves, and always exits
`:cq 23`; the harness never sends write confirmation. A blocked write, failed
validation, unstable oracle, modified artifact or incomplete cleanup makes the
run fail. This finite workload guard is not an arbitrary-SQL/function sandbox.
Reports contain metadata, digests and aggregate metrics, never row values,
credentials or screenshots. Owned private editor files are removed after each run.

## Compare explicit builds

For one Rust build, use `--worker /path/to/dbcli-rust` and optionally
`--frontend /path/to/dbcli-ui-rust` (UI command only). To compare multiple backends
within randomized rounds, supply a JSON manifest:

```json
[
  {"name":"before", "protocol":"dispatch", "worker":"/path/to/before/dbcli-rust"},
  {"name":"after", "protocol":"dispatch", "worker":"/path/to/after/dbcli-rust"}
]
```

Pass `--manifest /path/to/candidates.json`; relative worker paths resolve against
that file's directory. Backend runs include `python-reference` unless narrowed
with `--candidates before after`. Native protocols `dispatch`, `batch`, `rewrite`
are supported. UI runs use the local Ratatui frontend with the matching protocol
alias; an optional manifest `backend` field selects another compatible alias,
including Go aliases. This does not automatically add another language's frontend.
No comparison branches are scanned or changed.

## Offline and disposable checks

```bash
.local/performance-venv/bin/python -m unittest discover \
  -s benchmarks/quotes_perf -p 'test_*.py' -v
```

The regression tests use synthetic packets/screens/processes only. Live export
replay tests use the strictly validated disposable fixture described in
[RUST.md](../../RUST.md). Test fixtures and private row values must never be
committed as benchmark evidence. Historical comparison results remain in their
original worktrees; new outputs are separate.
