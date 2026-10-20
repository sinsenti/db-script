# db-script

A keyboard-driven terminal browser for SQLite, PostgreSQL and MySQL.

## Usage

```sh
python db.py path/to/database.sqlite
python db.py --read-only path/to/database.sqlite
python db.py postgresql://user:password@host:5432/database
```

Install optional native drivers with `pip install 'db-script[postgres,mysql]'`.
Direct PostgreSQL and MySQL URLs use the native drivers when available; use
`--cli` to explicitly select the `psql`/`mysql` fallback. The Docker and CLI
PostgreSQL browsers list tables from user schemas and show schema-qualified names
(for example, `app.leads`).

Run tests with:

```sh
python -m unittest discover -s tests -q
```

Optional native-adapter integration tests use the disposable Docker fixtures
in `tests/docker-compose.yml`; see `tests/test_integration_adapters.py`.

## Rust port

A Rust SQLite CLI/browser is available in `rust/`. Build and launch it with
`cd rust && cargo build --release && ./target/release/dbcli-rust path/to/database.sqlite`.
It supports SQLite table browsing, filters/sorts, schema and foreign-key
navigation, primary-key-safe CRUD, bounded read-only queries, JSON editing and
exports. The benchmark JSONL protocol also implements the selected native
PostgreSQL/MySQL workloads using the shared fixture contract; the interactive
Rust browser remains SQLite-only. Python remains the reference and the
multi-engine application. See [`rust/README.md`](rust/README.md) for controls,
remote protocol setup and parity checks.
