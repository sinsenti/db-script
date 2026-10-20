# Agent context: db-script

This file provides reusable context for agents working anywhere in this repository.
Keep it aligned with the implementation; it is not a task log or a replacement for tests.

## Project overview

- A keyboard-driven terminal database browser for SQLite, PostgreSQL and MySQL/MariaDB.
- Python 3.10+, packaged with setuptools via `pyproject.toml`.
- Both `python3 db.py ...` and the installed `dbcli` console command call `db.main`.
- `prompt-toolkit` is the runtime dependency. Native database drivers are optional:
  `psycopg` through the `postgres` extra and `mysql-connector-python` through `mysql`.
- The terminal UI uses ANSI escape sequences, `termios`, `tty` and `select`;
  assume a POSIX terminal, not native Windows support.
- Continue the incremental Python refactor. The roadmap only considers a Go/Rust
  rewrite after measuring performance and distribution needs.

## Setup and verification

Run commands from the repository root. Use `python3` if `python` is unavailable.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
# Optional native backends:
python3 -m pip install -e '.[postgres,mysql]'

# Default offline suite; explicitly prevent environment-enabled live DB tests:
DBCLI_RUN_INTEGRATION=0 python3 -m unittest discover -s tests -q
# Example focused tests:
python3 -m unittest tests.test_sql_helpers tests.test_sqlite_adapter -v
# Noninteractive entry-point checks:
python3 db.py --help
python3 db.py --version
```

For a manual UI check, use `python3 db.py --read-only /path/to/disposable.sqlite`
in a real terminal. Do not use a personal or production database as a test fixture.
Even with an explicit target, startup also performs local-file and Docker discovery.

The suite uses standard-library `unittest`, not pytest. There is no configured
lint/format/type-check toolchain or CI workflow. The `dev` extra currently installs
only the two native drivers.

### Optional integration tests

Read [`tests/test_integration_adapters.py`](tests/test_integration_adapters.py)
and [`tests/docker-compose.yml`](tests/docker-compose.yml) before running them.
They require Docker, installed drivers, `DBCLI_RUN_INTEGRATION=1`, and explicit
`DBCLI_POSTGRES_URL` / `DBCLI_MYSQL_URL` settings. The Compose fixtures publish
PostgreSQL on localhost port 55432 and MySQL on localhost port 53306.

These tests create, mutate and drop tables. Use only the disposable fixtures;
never point the environment variables at a real database. The test module documents
startup, URLs and cleanup. Its `down -v` cleanup removes fixture volumes. Report
skips separately from passes; an unavailable server can also cause a skip.

## Repository map

| Path | Responsibility |
| --- | --- |
| `db.py` | Argument parsing, target selection, database/table menus, adapter selection and callbacks passed into the browser. Still contains substantial controller wiring. |
| `dbcli/adapters/base.py` | `DatabaseAdapter` contract, `QueryResult` and shared errors. |
| `dbcli/adapters/sqlite.py` | SQLite metadata, paging, CRUD and guarded read queries using the standard library. |
| `dbcli/adapters/dbapi.py` | Shared native PostgreSQL/MySQL DB-API implementation; injectable connection factories. |
| `dbcli/adapters/postgres.py`, `mysql.py` | Lazy driver imports, connection configuration and dialect-specific metadata. |
| `dbcli/query.py` | Identifier quoting, filter classification/composition, sort validation, pagination and primary-key predicates. |
| `dbcli/ui.py` | Terminal lifecycle, rendering, input, autocomplete, menus, row/table browsing, bulk-edit diff preview and clipboard helper. |
| `dbcli/editor.py` | External-editor handoff and temporary SQL/JSON files; honors `$EDITOR`, then tries nvim, vim and nano. |
| `dbcli/export.py` | JSON, CSV and SQL INSERT exports; returns output paths and defaults to writing in the current directory. |
| `dbcli/discovery.py` | Shallow current-directory SQLite signature detection and injectable Docker discovery. |
| `dbcli/legacy.py` | Subprocess-backed direct/Docker PostgreSQL/MySQL paths and the index/constraint/trigger inspector. |
| `tests/` | SQLite fixtures, native-adapter fakes, helper/module tests and opt-in live adapter tests. |
| `todo.md` | Broad development roadmap; checkbox status can lag behind the code. |

## Connection flow and contracts

- SQLite targets use `SQLiteAdapter`. Direct PostgreSQL/MySQL URLs prefer native
  adapters; `--cli` or a missing optional driver selects the legacy `psql`/`mysql`
  path. `create_native_adapter` catches `DriverUnavailable`, not arbitrary failures.
- Discovered Docker databases still use `docker exec` and legacy helpers, not native
  adapters. The native PostgreSQL adapter defaults to `public`; legacy Docker/CLI
  PostgreSQL browsing lists non-system schemas with schema-qualified table names.
  MySQL uses the URL database.
- `browse_table` consumes callbacks and an optional `adapter` bridge. Preserve
  callback compatibility while refactoring; do not move connection management into
  UI rendering. Foreign-key navigation opens nested browsers for the target table.
- `QueryResult` contains `headers: list[str]` and `rows: list[tuple[...]]`.
  `.as_tuple()` bridges to legacy `(headers, rows)` callbacks. Legacy CLI output may
  contain strings where native drivers return typed values.
- Adapter mutations return affected-row counts, not booleans. Zero can be a no-op;
  UI bridges generally treat exactly one affected row as success. Expected failures
  use `AdapterError` subclasses, including `ReadOnlyError` and `RowIdentityError`.
- Adapters use short-lived connections per operation. SQLite enables foreign keys
  on every connection. Use temporary on-disk SQLite fixtures: `:memory:` does not
  preserve state across separately opened connections.
- Keep optional drivers lazy and injectable so SQLite-only imports and offline
  native-adapter tests do not require those drivers or a server.

## SQL and data-safety rules

- Bind generated values (`?` for SQLite, `%s` for native PostgreSQL/MySQL). Quote
  identifiers separately with `quote_identifier`; validate sort columns/directions
  and page bounds through the shared helpers. Do not copy legacy SQL interpolation
  into new adapter code.
- Native/SQLite update and delete paths identify rows from the original primary-key
  values, including composite keys. Missing keys must remain an error, not a reason
  to fall back to all-column matching. Validate supplied column names.
- Plain-text filters become bound cross-column `LIKE` searches. Inputs classified
  by `is_sql_where_expression` are deliberately inserted as raw SQL predicates.
  That classifier and `sanitize_where_clause` are **not** SQL parsers or security
  sanitizers. Keep `fetch_page` and `count_rows` filter semantics consistent.
- `execute_readonly` is intended for bounded SELECT/WITH/EXPLAIN results. SQLite
  uses an authorizer; native DB-API adapters use statement/keyword checks. Do not
  advertise these checks as a sandbox for untrusted SQL or side-effecting functions.
- `--read-only` hides/disables UI mutations and makes adapter CRUD reject writes.
  SQLite opens a read-only URI, but native adapters do not configure server-enforced
  read-only transactions; legacy paths are weaker still. Use database permissions
  when a hard read-only guarantee is required. Remote sessions are writable by default.
- Preserve JSON object/array round-tripping in editors, adapters and exports. SQL
  literal formatting for an export or relation filter is not a write-query API.
- Do not expose credentials in logs, documentation or new subprocess arguments.
  Existing legacy commands can expose passwords; do not propagate that pattern.

## Current UI behavior and known gaps

Use `dbcli/ui.py` as the source of truth for keybindings, not older planning notes.

- Navigation: `j/k`, `H/L` column focus, `Enter/l` row detail, `gg/G`, `Ctrl+D/U`,
  and `h/q` back. `/` filters; `:` accepts SQL, filter expressions or row numbers;
  `Ctrl+G` edits filter/prompt text in the external editor.
- `o/O` sorts, `v` toggles column visibility, Space marks rows, and `f` follows
  foreign keys. `a`, `e`, `E`, `dd` insert/edit/bulk-edit/delete in writable sessions.
- `x` exports JSON; `X` offers JSON/CSV/SQL. Without marked rows, exports fetch up
  to a hard-coded 100,000 rows, not an unlimited or streaming result set.
- `S` currently displays only the first schema row. `I` is implemented, but routes
  through the legacy inspector even with a native adapter, so remote inspection
  still needs the relevant CLI client.
- The clipboard helper tries `wl-copy`, `xclip`, then `pbcopy`, but `y`/`yy` actions
  are not wired. Persistent SQL/filter history and editor-based history selection
  are not implemented.
- Bulk editing already has a diff/confirmation screen, but is not transactional
  across rows. It fetches up to 100,000 rows, matches by `id` or heuristics, and can
  infer deletions from missing edited rows. Do not mistake the preview for complete
  mutation safety or extend this behavior without reviewing identity/confirmation.
- Adapters currently commit mutations before the UI checks the affected-row count;
  unexpected multi-row changes are not automatically rolled back. Legacy writes
  also lack the native adapters' primary-key and parameter-binding guarantees.
- Backups, consistent subprocess timeouts/cancellation and robust Unicode-width
  rendering remain gaps. Packaging already exists despite unchecked roadmap items.

## Making changes

1. Check the working tree and preserve unrelated changes, including local notes.
2. Read the relevant implementation and tests. Treat roadmap items as intentions,
   not proof that a feature exists or is missing.
3. Prefer small changes in the owning module. Keep `db.py` entry-point compatibility,
   avoid new UI/editor import cycles, and follow existing typing/style conventions.
4. Add regression tests alongside changes. Use temporary files for SQLite/exports,
   injected connection factories for native adapters, and mocked subprocess/editor
   calls. Native fakes do not validate real-server SQL or driver behavior; the
   interactive UI and legacy database paths currently lack dedicated test coverage.
5. For UI changes, check callback wiring and read-only propagation in SQLite,
   native direct URLs, CLI fallback, Docker and nested foreign-key/query screens.
   Preserve terminal/cursor restoration and temporary-file cleanup on failures.
6. Run focused tests and the offline suite; state what was not exercised. Only run
   manual/live checks against disposable databases when appropriate to the task.
7. Update this file and user-facing docs when behavior, commands or architecture
   change. If changing the version, keep `db.APP_VERSION` and `pyproject.toml` aligned.
