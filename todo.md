# Database browser development plan

Goal: turn `db.py` into a safe, tested, installable terminal database browser. Keep Python for the next stage; only consider a Go/Rust rewrite after measuring the refactored version.

## Phase 1 — Refactor the Python application

- [x] Keep `db.py` as the executable entry point so existing usage continues to work.
- [x] Create a `dbcli/` package for reusable application code.
- [x] Define shared result types and adapter errors in `dbcli/adapters/base.py`.
- [x] Move common SQL composition concerns into `dbcli/query.py`.
- [x] Add safe identifier quoting and validated sort-column handling.
- [x] Keep the UI independent from the database implementation through an adapter contract.
- [x] Extract low-level terminal rendering, input, styling and clipboard helpers into `dbcli/ui.py`.
- [x] Move higher-level menus and browser screens from `db.py` into `dbcli/ui.py`.
- [x] Move export functions and database discovery into separate modules.
- [x] Move editor integration into `dbcli/editor.py`.
- [x] Remove migrated SQLite wrappers and isolate the remaining Docker/`--cli` compatibility paths in `dbcli/legacy.py`.

## Phase 2 — Database adapters and tests

- [x] Define the common `DatabaseAdapter` interface.
- [x] Implement a tested `SQLiteAdapter`.
- [x] Use parameter binding for SQLite values and primary-key-based mutations.
- [x] Refuse update/delete operations on tables without a primary key.
- [x] Enable SQLite foreign-key enforcement on every connection.
- [x] Add read-only query execution with a bounded result set.
- [x] Migrate the SQLite browser flow in `db.py` to the adapter.
- [x] Add unit tests for filtering, sorting, pagination, CRUD, JSON values, foreign keys, read-only mode and composite-key helpers.
- [x] Implement a native `PostgresAdapter` using optional `psycopg`.
- [x] Implement a native `MySQLAdapter` using optional `mysql-connector-python`.
- [x] Keep the existing CLI/subprocess path as an explicit `--cli` fallback and as a dependency fallback.
- [x] Add opt-in PostgreSQL/MySQL integration tests for Docker-published services.
- [x] Add adapter contract tests for SQLite, PostgreSQL and MySQL adapter behavior.

## Phase 3 — Safety and reliability

- [ ] Parameterize all generated PostgreSQL/MySQL values.
- [ ] Quote identifiers separately from values for every dialect.
- [ ] Detect primary keys, including composite keys, on every backend.
- [ ] Check affected-row counts and refuse unexpected multi-row mutations.
- [ ] Make bulk editing transactional and add a dry-run preview.
- [ ] Disable bulk editing above a configurable row limit.
- [ ] Never treat a missing bulk-edit row as a delete without explicit confirmation.
- [x] Add a `--read-only` session mode that hides mutation actions.
- [ ] Make remote connections read-only by default.
- [ ] Stop putting database passwords in process command-line arguments.
- [ ] Add subprocess timeouts, cancellation and structured error messages.
- [ ] Add backups before destructive operations.

## Phase 4 — User experience

- [x] Add command-line parsing foundation with positional targets, `--help`, `--version` and `--read-only`.
- [ ] Add `--limit`, `--query`, `--format` and `--no-color`.
- [ ] Add a full-screen multiline SQL editor.
- [ ] Add searchable query history per connection.
- [ ] Add saved queries/bookmarks.
- [ ] Use the existing clipboard helper for copying cells, rows and tables.
- [ ] Fix schema display so all schema rows are browsable, not only the first row.
- [ ] Add loading indicators and responsive cancellation for slow queries.
- [ ] Add better Unicode-width-aware table rendering and terminal resize handling.

## Phase 5 — Database tools

- [ ] Add `EXPLAIN` / query-plan views for PostgreSQL, MySQL and SQLite.
- [ ] Add query timing and optional slow-query warnings.
- [ ] Add views, schemas, sequences, procedures and materialized views where supported.
- [ ] Add CSV/JSON import with validation and transaction rollback.
- [ ] Add schema diff and foreign-key/ER diagram views.
- [ ] Add SQLite integrity check, backup and restore actions.
- [ ] Add PostgreSQL locks/activity and MySQL process/performance views.

## Phase 6 — Distribution and quality

- [ ] Add `pyproject.toml` and an installable console command.
- [ ] Add README usage examples and keybinding documentation.
- [ ] Add linting, formatting and static type checking.
- [ ] Run unit tests in CI on Linux, macOS and Windows where practical.
- [ ] Add test fixtures and safe demo databases.
- [ ] Benchmark Python startup, rendering and query latency.
- [ ] Build a small Go/Rust proof of concept only if distribution or measured performance justifies it.
- [ ] Compare the proof of concept against Python before deciding on a rewrite.
