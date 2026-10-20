# PostgreSQL read-only interactive browser

## Run

Set `DBCLI_POSTGRES_URL` securely in the environment, then run from the repository root:

```sh
cargo build --release --locked --manifest-path rust/Cargo.toml
rust/target/release/dbcli-rust --postgres
rust/target/release/dbcli-rust --postgres --table dbcli_alt.children
```

The table menu lists schema-qualified base tables in every accessible non-system schema. Unqualified names mean `public`; names with punctuation/spaces can be quoted as `schema."Mixed Table"`. No credentials are accepted in the new command or printed by its connection diagnostics. Existing SQLite startup, writable-by-default CRUD, atomic batches, bundled SQLite/FTS5, discovery and editor behavior remain available.

## Reads and interaction

The existing native Rust terminal controller now uses PostgreSQL for paging, plain-text or expression filters, validated sort, jump/navigation, full row cards, column visibility, stable marks, schema and index/constraint/trigger inspection, raw nested results and JSON/CSV/SQL export. `f` offers an FK chooser; composite keys use all paired columns, and cross-schema relationships retain the referenced schema. Nested screens retain the same read-only policy. `a/e/E/dd` are disabled; adapter mutations reject writes even without `--read-only`.

`/` and `:` retain fuzzy/cycling Tab completion and Ctrl+G editor handoff. PostgreSQL completion adds typed scalar examples and PostgreSQL JSON `->`/`->>` paths from bounded current-page samples; SQLite-only `json_extract` suggestions are removed. Completion is contextual, not a complete SQL grammar. Existing `$EDITOR` handling, private temporary files, cleanup and terminal restoration are retained.

Exports retain the active filter/sort or marked snapshots, capped at 100,000 rows without marks. JSON preserves native structured JSON, nested arrays, byte tags and typed scalar envelopes. CSV uses human-readable scalar text and cannot encode all database storage types. SQL exports use quoted schema/table/column identifiers, PostgreSQL bytea literals, exact numeric text, explicit datetime/UUID casts, and source result types to distinguish JSON arrays from PostgreSQL arrays. Raw-query SQL exports name a placeholder `query_result` destination; create/choose a matching destination before replay. Exports write local files only.

## Connection policy

Every operation opens a fresh native `postgres` connection. Startup options force `default_transaction_read_only=on`, UTC, a statement timeout no greater than 3 seconds and lock timeout no greater than 1 second; positive stricter millisecond/integer-second URL settings are retained. Connect timeout is capped at 3 seconds. Each operation explicitly begins `READ ONLY`, verifies actual/default read-only settings, UTC and timeout bounds, and only then executes application metadata/reads. Closing the operation rolls back its read transaction. No mutation probe is needed to establish this policy.

The native driver currently uses `NoTls`: servers requiring TLS are unsupported and fail with a generic diagnostic. SQL/predicate checks are convenience guards, not a sandbox for untrusted functions or SQL. Database permissions remain useful defense in depth. Server-enforced read-only transactions prevent persistent database writes; they do not make arbitrary functions harmless outside the database.

## Values and protocol

The new `pg_read.rs` policy, SQL and binary codec module is shared across these three Rust upgrade branches. Their preexisting UI controllers, protocol envelopes, SQLite adapters and MySQL drivers remain distinct. This PostgreSQL phase is not three independent adapter designs.

The decoder handles NULL, bool, int2/int4/int8/OID, float4/float8, UTF-8 text, UUID, exact numeric (including scale), date/time/timetz, timestamp/timestamptz, JSON/JSONB, bytea, domains, enums and rectangular multidimensional arrays with NULL elements. Unsupported native types fail explicitly. Values such as interval, ranges, records and geometric/network extensions are not implemented. UTC aware timestamps use ISO text with `+00:00`; naive timestamps have no offset. Scalar envelopes are `{"$type":"uuid"|"decimal"|"date"|"time"|"datetime","value":"..."}`. PostgreSQL JSON is decoded as JSON, including objects resembling byte tags; byte-tag conversion uses actual result types.

The original JSONL interface is `--postgres --protocol`, preserving `{ok,result,error,trace}`. It accepts scalar operations `list_tables`, `schema`, `primary_keys`, `foreign_keys`, `navigation_foreign_keys`, `page`, `count`, `raw`, `raw_page`, `raw_count`, `inspector`/`indexes`, `schemas`, and read-only-rejected mutations. Existing table/filter/sort/limit/offset/sql/params fields remain. Raw binds accept native `$n` and `%s` templates outside strings/comments; arbitrary SQL without parameters is unchanged. Bytea uses the original `{"$blob":"base64"}` wire envelope. `foreign_keys` returns flat compatible metadata; navigation additionally carries grouped composite pairs.

## Validation

```sh
cargo test --locked --manifest-path rust/Cargo.toml
python3 rust/tests/writable_pty.py rust/target/release/dbcli-rust --kind original
DBCLI_RUN_INTEGRATION=0 python3 -m unittest discover -s tests -q
```

Pure regressions cover binary numeric small fractions/large decimals, UUID/date/time, nested arrays/NULLs/truncation, JSON byte-tag provenance, quoted identifiers, lexical bindings and SQL guards, startup policy overrides, malformed URL redaction, and mutation rejection before connecting. Independent fixture QA covers typed reads, server settings and guarded views, unchanged fixture snapshots, composite/cross-schema FKs, inspectors and real PTY read workflows. The root comparison harness records actual live read-only checks separately. No real user database is written by these checks. Unicode column widths and persistent prompt history retain the earlier UI limitations.
