# Selected PostgreSQL / MySQL comparison protocol

This is the shared PostgreSQL/MySQL extension to the SQLite protocol. Go and Rust implement the native adapter workloads in protocol mode; Python production adapters remain the oracle. These remote adapters are for the selected comparison workload, not interactive/general-purpose remote browsing.

## Safety boundary and service URLs

Only use the disposable Compose services from `tests/docker-compose.yml`: PostgreSQL at `127.0.0.1:55432/postgres` and MySQL at `127.0.0.1:53306/test`. `remote_fixture.py` refuses any other host, port, database or username/password pair. The Go/Rust/Python runner receives credentials only through `DBCLI_POSTGRES_URL` and `DBCLI_MYSQL_URL` environment variables, never argv. Both native runners validate the exact approved endpoint, and `remote_fixture.py` independently validates it before any reset. Do not print or persist these values. Fixture reset/drop touches only the fixed `dbcli_cmp_*` tables/views in those disposable databases.

Start services from the repo root:

```sh
docker compose -f tests/docker-compose.yml up -d
.venv/bin/python -m pip install -e '.[postgres,mysql]'
```

The Python oracle uses the production `PostgresAdapter`/`MySQLAdapter` with an instrumented DB-API connection wrapper. Protocol mode is `--protocol --engine postgres|mysql`; secrets are read from the engine-specific environment variable. The remote fixture is reset before each candidate and mutation workload. Cleanup drops only its fixed fixture objects.

## Placeholder and result contract

Requests in `remote_workloads.json` are shared for both engines/candidates. The `raw.sql` value uses canonical `?` bind markers; the engine runner translates to the native driver form. Trace SQL is normalized *only* for placeholder spelling (`?`, psycopg `%s`, PostgreSQL `$1`/`$2`, all canonicalized by bind order); the actual query shape, identifier quoting, clause order and bound values are compared. This documented translation reflects driver APIs; no other SQL normalization is allowed. Compare ordered rows, headers, schema metadata, affected counts, post-mutation state, error class/message, and typed/blob-normalized values.

The selected remote workload covers connection/startup, table/view discovery, schema/FK discovery, paging, plain-text and expression filters, count, sorting, bound raw SELECT, primary/composite-key CRUD, read-only CRUD rejection, and bounded raw-query rejection. As in the Python reference, network latency can dominate these adapter operations; report connection/startup and remote SQL timing separately from local SQLite and do not attribute server/round-trip latency to language runtime.

## Commands

```sh
# Set the two explicit disposable URLs in your environment, then:
.venv/bin/python benchmarks/language_comparison/remote_parity.py \
  --go-bin go-sqlite/build/dbcli \
  --rust-bin ../codex-rust-sqlite-benchmark/rust/target/release/dbcli-rust

# Remote timing is permitted only after all candidates pass remote parity,
# SQLite three-way parity passes, and the shared /tmp timing lock is acquired:
.venv/bin/python benchmarks/language_comparison/remote_benchmark.py \
  --go-bin go-sqlite/build/dbcli \
  --rust-bin ../codex-rust-sqlite-benchmark/rust/target/release/dbcli-rust

docker compose -f tests/docker-compose.yml down -v
```

`remote_benchmark.py` requires successful three-candidate remote parity and three-candidate SQLite parity, plus the shared exclusive timing lock. Do not benchmark until all candidates are release-ready and the timing window is explicitly coordinated. If Docker/drivers/services are unavailable, report a skip/blocker and its exact external cause separately from passes. Never set these environment variables to non-disposable endpoints.
