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

`a` inserts, `e` edits one row, and `dd` deletes after confirmation. `E` opens a
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

## Source selection

- Backend: `codex_rust_quotes_original`, commit `c51eae7fa2b32a23c84ce64e45fd36ecfaf378ff`.
- Ratatui: `codex_rust_quotes_neovim`, commit `24fc85ebd8b893ff0b1ac5b1987ba93910e97c80`.
- Docker connection helper: `codex_codex_quotes_perf`, source commit `908bf7d`.

The backend was the fastest measured full-table reader (128.2 ms for 771 quotes)
and had the lowest measured bulk worker CPU. The full Rust bulk UI still uses more
memory than the Go/Python UIs; selection does not imply a win on every metric.
Comparison branches, binaries and reports remain preserved in their worktrees.

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
