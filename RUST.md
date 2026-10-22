# Rust database browser

Main includes the **Original Rust backend** selected from the PostgreSQL `quotes`
comparison, plus its matching Ratatui interface. Python remains available through
`db.py --python` / `dbcli --python`. SQLite and PostgreSQL sessions are writable by default.

## Build

Rust/Cargo and a C compiler are required. The exercised toolchain was Rust 1.98.1.
SQLite is bundled. Neovim must be on PATH for `E` bulk editing.

```bash
bash tools/build_rust.sh
```

## Start

The standard entry command now opens the discovery picker, then Rust:

```bash
python ~/git/personal/db-script/db.py
python ~/git/personal/db-script/db.py --read-only
```

The title is **Interactive Database Browser & Explorer**. It lists current-directory
SQLite files, running database containers, an optional PostgreSQL environment URL,
manual SQLite path entry and refresh. PostgreSQL containers offer accessible
databases before Rust's table browser. Closing Rust returns to this picker.
Docker PostgreSQL requires a local published port and resolvable credentials;
credentials stay in memory/environment and no Python driver is required.
MySQL entries are explicitly labeled Python. Use `--python` for the entire original
Python experience, or `--cli` for its legacy client paths. Missing native binaries
show a build hint instead of silently changing supported targets to Python.

Explicit native launchers remain available:

```bash
# Your running Docker PostgreSQL, opening quotes directly:
python3 tools/rust.py --postgres --table public.quotes

# Existing SQLite database:
python3 tools/rust.py --database ~/.timetrace/timetrace.db

# Explicit read-only session:
python3 tools/rust.py --postgres --table public.quotes --read-only

# Original terminal interface over the same backend:
python3 tools/rust.py --postgres --table public.quotes --legacy
```

The Docker default is `a1labs-postgres-1`; use `--container NAME` to change it.
For an existing `DBCLI_POSTGRES_URL`, pass `--from-env`. Credentials are resolved
in memory and passed in the environment. No comparison worktree is required.
The Python launcher uses only the standard library; native binaries can run
without Python when credentials are already configured:

```bash
./ui/rust/target/release/dbcli-ui-rust --engine postgres \
  --worker "$PWD/rust/target/release/dbcli-rust" --table public.quotes
./rust/target/release/dbcli-rust --db ~/.timetrace/timetrace.db
```

## Editing

`a` inserts, `e` opens the current row directly in `$EDITOR` (then nvim/vim/nano),
and `dd` deletes after confirmation. Saving/exiting the single-row editor returns
to explicit review; `y` confirms, and unchanged/cancelled edits perform no writes.
`E` opens a
private Neovim diff: original on the left, editable plan on the right. Use `zR`
to open folds, `:wqa` to return to operation-count confirmation, or `:cq` to cancel.
The editor's `original` fields contain only the complete primary key, for example
`{"id":1}`; `row` still contains every editable field. Deletions list these same
key objects explicitly. Keep key tokens unchanged. Full original snapshots stay
in application memory and are restored into confirmed actions for conflict checks.
This reduces duplicated JSON in the two diff files, not the 100,000-row in-memory
bulk limit. Unchanged edits dispatch no writes. Confirmed bulk changes run atomically; omitted
update entries never imply deletion. Use `DBCLI_BULK_EDITOR='nvim --clean'` for a
clean Neovim profile. Single-row/filter/SQL editors honor `EDITOR`.

See [backend controls](rust/WRITABLE_SQLITE.md), [PostgreSQL contracts](rust/POSTGRES_WRITABLE.md)
and [Ratatui controls](ui/rust/README.md). Bulk editing is bounded to 100,000 rows
and holds data in memory. PostgreSQL TLS-required endpoints and interactive MySQL
browsing are unsupported. Raw SQL remains read-only in writable sessions.

## Schema diagram

Press **D** in either pane (or Ctrl+P → `diagram`) to open an offline, interactive
database diagram in your web browser. It includes schema grouping, columns/types,
PK/FK/unique keys, column-level foreign-key links, search, table dragging,
zoom/pan, and a selected-table/neighbours view. Click a relationship for composite
column mappings, cardinalities and ON UPDATE/DELETE actions. Export the current
scope as SVG or Mermaid; **Save HTML** preserves the full offline snapshot.

This uses read-only metadata queries for SQLite/PostgreSQL, with no row data,
connection credentials, web server or online dependency. Esc cancels collection.
The TUI displays the private file path if automatic desktop opening fails.
Private snapshots are stored in **`~/dbcli-diagrams/schema-*/index.html`** so Snap
browsers can read them. They survive later D requests and application exit;
delete old snapshot folders when no longer needed. Save HTML makes a portable
copy. No relationships are inferred from JSON or
application code. See [the UI guide](ui/rust/README.md#database-relationship-diagram)
for permissions, metadata scope, declared cardinalities and size limits.

## Filter completion

Grid `/` suggests columns, predicates and short cached values. Use ↑/↓ to select,
Tab to accept. Enter accepts JSON suggestions or an arrow-selected suggestion;
otherwise it submits the filter as typed. Ctrl+R always applies the filter. Esc hides suggestions
before closing the prompt; Tab reopens them. Completion respects the cursor and
preserves surrounding clauses. Values come only from the loaded page; no table
scan or extra query runs while typing. Sidebar `/` remains table-name search.
Choose a JSON column with Tab/Enter to browse its immediate observed fields,
then descend through objects/arrays. Accepting a leaf inserts SQLite `json_extract`
or PostgreSQL `#>>` SQL. Typing a prefix such as `payload.` works too.
The path shorthand must be completed before submission. PostgreSQL extraction
returns text; cast explicitly for numeric comparisons. Empty/unseen keys and
oversized JSON documents are outside the bounded discovery sample.
See [the UI guide](ui/rust/README.md#filter-suggestions) for examples and limits.

## Cancelling reads and worker limits

In the default Ratatui browser, **Esc cancels a pending read without exiting**.
If a modal is open, the first Esc closes the modal; the next cancels the read.
You can also submit a new `/` filter, `:` SQL query, or sidebar table selection
while a read is running. The newer request supersedes the earlier one. Replies
carry UI request IDs; stale successes and errors cannot change rows, status,
busy state, or open a bulk editor/export prompt. Ctrl+P → `refresh` retries the
current view (or table discovery if it was cancelled at startup).

For the selected Original Rust PostgreSQL worker, cancellation first sends the
current request ID over a private inherited Unix socket. The worker uses the
driver's CancelToken to attempt a PostgreSQL CancelRequest for that read. The UI
allows up to 350 ms for this attempt, then terminates/reaps the worker and closes
its database connection. PostgreSQL does not acknowledge whether cancellation
succeeded; disconnect detection and existing server timeouts remain fallbacks.
Late IDs cannot target a newer read, and writable connections never register a
read-cancellation token. Other backend/engine combinations discard the worker
without this driver-level attempt. The next read starts a fresh worker; a partial
or late protocol frame is never reused.

The native UI bounds JSONL transport before parsing each response:

| Environment variable | Default | Accepted range |
| --- | --- | --- |
| `DBCLI_WORKER_MAX_RESPONSE_BYTES` | 134217728 (128 MiB) | 1–1073741824 bytes |
| `DBCLI_WORKER_MAX_REQUEST_BYTES` | 134217728 (128 MiB) | 1–1073741824 bytes |
| `DBCLI_WORKER_DEADLINE_MS` | 60000 (60 seconds) | 1–600000 ms |

The deadline covers the complete nonblocking request write and response read;
it also bounds startup handshakes. Receiving occasional bytes does not reset it.
Frames are measured without the terminating newline. Oversized/truncated/invalid
responses, blocked writes and expired deadlines produce an error, discard the
worker and leave the browser available for another request. Invalid settings fail
before spawning a worker. Limits bound serialized transport, not total application
or backend memory; the 100,000-row bulk/export limit still applies separately.
These settings apply to Ratatui's worker bridge, not the synchronous `--legacy`
interface or Python reference browser.

Confirmed writes cannot be cancelled or superseded by a newer read. They have the
same I/O deadline; a timeout or forced exit may leave their outcome unconfirmed.
The UI reports this explicitly and never automatically retries a write. Refresh
and inspect the database before retrying. PostgreSQL statement/lock timeouts and
atomic mutation validation remain in effect independently.

## Verification

```bash
cargo test --locked --manifest-path rust/Cargo.toml
cargo test --locked --manifest-path ui/rust/Cargo.toml
python3 -m unittest discover -s tools -p 'test_*.py' -q
DBCLI_RUN_INTEGRATION=0 python3 -m unittest discover -s tests -q
```

Mutation tests use disposable fixtures only. Live application databases must not
be used as mutation fixtures. Optional PostgreSQL Neovim checks can use the marked
fixture managed by `tools/postgres_write_fixture.py`; see the backend/UI test docs.

The current-main PostgreSQL acceptance script covers CRUD, atomic rollback, typed
values, read-only controls, driver cancellation, stale/queued IDs, protected writes,
and actual UI cancellation/superseding/deadline flows. After building both Rust
executables and installing the benchmark development requirements, run it only
against the marked disposable fixture:

```bash
python3 tools/postgres_write_fixture.py start
.local/performance-venv/bin/python ui/rust/tests/postgres_acceptance.py \
  --fixture "$PWD/.local/postgres-write/fixture.json"
python3 tools/postgres_write_fixture.py stop
```

`--cases` selects individual scenarios. The script rejects application endpoints,
creates unique owned synthetic schemas and drops them after each scenario.

## Source selection

The [comparison and outcome](COMPARISON.md) covers the earlier SQLite/native
experiments, the selection decision and follow-up changes. Full historical
[quotes and Neovim measurements](docs/comparison/quotes-2026-10-20.md) are preserved
in this checkout; those tables do not measure the latest main build.

- Backend: `codex_rust_quotes_original`, commit `c51eae7fa2b32a23c84ce64e45fd36ecfaf378ff`.
- Ratatui: `codex_rust_quotes_neovim`, commit `24fc85ebd8b893ff0b1ac5b1987ba93910e97c80`.
- Docker connection helper: `codex_codex_quotes_perf`, source commit `908bf7d`.

The backend was the fastest measured full-table reader (128.2 ms for 771 quotes)
and had the lowest measured bulk worker CPU. The full Rust bulk UI still uses more
memory than the Go/Python UIs; selection does not imply a win on every metric.
Comparison branches and reports remain preserved in Git. Removed Rust worktrees
and their release binaries have a private archive; see the
[review and recovery guide](docs/comparison/worktree-cleanup.md).

## Export destinations

JSON, CSV and SQL exports in both Rust interfaces create new files exclusively,
with private mode 0600. An existing file, directory or symlink is rejected; choose
another filename. Ratatui keeps the path prompt open on a collision; the legacy
interface offers a new export name (empty cancels). Failed writes remove their
newly created partial file. Existing
exports are never silently overwritten. This also applies in read-only database
sessions: exporting creates a local file but does not change the database.

## PostgreSQL SQL exports

`X` → SQL omits computed/generated columns so PostgreSQL recomputes them on replay.
Exported `GENERATED ALWAYS` identity values use `OVERRIDING SYSTEM VALUE`;
`GENERATED BY DEFAULT` identity values are retained normally. When no insertable
columns remain, each exported row becomes `INSERT ... DEFAULT VALUES`.
Both Ratatui and the legacy Rust interface use this behavior. JSON and CSV keep
all projected columns. SQL exports contain data INSERTs, not table definitions or
identity-sequence resets. Legacy standalone raw-query SQL exports still target
`query_result`, whose destination schema is supplied by the user.

## Performance tools

The [standalone benchmark guide](benchmarks/quotes_perf/README.md) provides setup,
backend/UI commands, explicit build comparisons and metric limitations. Defaults
use only main's binaries and reference Python adapters. Backend results must match
a PostgreSQL oracle before timing; UI runs use real Neovim and always cancel.

```bash
.local/performance-venv/bin/python benchmarks/quotes_perf/backend_bench.py \
  --table public.quotes --repetitions 9
.local/performance-venv/bin/python benchmarks/quotes_perf/ui_bench.py \
  --table public.quotes --interfaces modern legacy --repetitions 3
```

Export replay regression tests are opt-in and mutate only a marked disposable
fixture, using unique owned schemas. With the fixture manager from this checkout:

```bash
python3 tools/postgres_write_fixture.py start
DBCLI_TEST_POSTGRES_FIXTURE="$PWD/.local/postgres-write/fixture.json" \
  cargo test --locked --manifest-path rust/Cargo.toml --test postgres_exports
DBCLI_TEST_POSTGRES_FIXTURE="$PWD/.local/postgres-write/fixture.json" \
  DBCLI_EXPORT_TEST_WORKER="$PWD/rust/target/release/dbcli-rust" \
  cargo test --locked --manifest-path ui/rust/Cargo.toml --test postgres_exports
python3 tools/postgres_write_fixture.py stop
```

Tests without the fixture variable report the live replay check as skipped in
captured output. Application databases must never be used as mutation fixtures.


## Picker terminal checks

The offline startup regressions are in `tests/test_launcher.py`. Optional real
PTY checks create disposable SQLite files and exercise picker → Rust → picker,
manual file entry, writable/read-only propagation, interruption and terminal
restoration. With the benchmark development environment (pyte installed):

```bash
DBCLI_RUN_PICKER_PTY=1 .local/performance-venv/bin/python -m unittest discover \
  -s tools -p test_picker_pty.py -v
```

To also exercise container → database → Rust with synthetic PostgreSQL rows,
start only the marked fixture managed by this checkout:

```bash
python3 tools/postgres_write_fixture.py start
DBCLI_RUN_PICKER_PTY=1 DBCLI_RUN_PICKER_POSTGRES_PTY=1 \
  .local/performance-venv/bin/python -m unittest discover \
  -s tools -p test_picker_pty.py -v
python3 tools/postgres_write_fixture.py stop
```

The PostgreSQL checks validate the private descriptor/container marker and create
and drop only unique owned fixture schemas. No application database is used.
`DBCLI_PICKER_PYTHON=/path/to/python` selects the interpreter used by the child
entry point; by default it is the test interpreter. Missing opt-in flags are skips.


Export refusal/retry can be checked in real terminals against temporary SQLite:

```bash
python3 ui/rust/tests/exports_pty.py rust/target/release/dbcli-rust --kind original
python3 ui/rust/tests/exports_pty.py ui/rust/target/release/dbcli-ui-rust \
  --kind ratatui --worker rust/target/release/dbcli-rust
```
