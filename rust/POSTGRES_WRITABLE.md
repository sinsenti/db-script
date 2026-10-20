# Writable PostgreSQL

This native Rust browser supports PostgreSQL browsing and bound CRUD through
`--postgres`. Set credentials in `DBCLI_POSTGRES_URL`; never put the URL in
command arguments. Sessions are writable by default. Add `--read-only` to reject
all mutations in the UI and adapters, including nested FK screens.

```sh
cargo build --release --locked --manifest-path rust/Cargo.toml
rust/target/release/dbcli-rust --postgres --table public.items
rust/target/release/dbcli-rust --postgres --read-only --table public.items
cargo test --locked --manifest-path rust/Cargo.toml
DBCLI_RUN_INTEGRATION=0 python3 -m unittest discover -s tests -q
```

SQLite keeps its existing writable/default behavior and bundled FTS5. Native MySQL protocol behavior is unchanged; interactive MySQL browsing is not supported.

## Connections and transactions

Every metadata, page, count and raw read opens a short-lived native connection with
both default and actual transaction read-only on. Mutation connections separately
force default read-only off, explicitly BEGIN READ WRITE, verify that mode, and
commit only after every action succeeds. Prior read-only URL options cannot
silently force a writable session into the wrong mode. UTC, connect timeout at
most 3s, statement timeout at most 3s, and lock timeout at most 1s are verified;
positive stricter URL settings are retained. SQLSTATE diagnostics omit server
messages and connection details. No automatic retry follows a failed/ambiguous
COMMIT; refresh before deciding whether to retry.

Raw SELECT/WITH/EXPLAIN always uses an actual server read-only transaction, even
when the browser is writable. Lexical checks are convenience guards, not a
sandbox for arbitrary functions. NoTls local PostgreSQL16 is the exercised
transport; TLS-required endpoints remain unsupported.

## Writes and editors

`a` accepts one object or an array, `e` edits the current original row, `E`
uses explicit `{updates:[{original,row}],insert:[],delete:[]}`, and `dd`
deletes the current or marked rows. PostgreSQL insert/edit opens a scrollable
transaction preview: finish with q, then y + Enter to apply. Bulk E uses a private Neovim diff, then count-only confirmation (see [BULK_NEOVIM.md](BULK_NEOVIM.md)); explicit deletion confirmations remain. Omitted edited rows never imply deletion.

Generated/default/identity columns are omitted from insert templates; supply
optional defaults manually if desired. Generated/GENERATED ALWAYS identity values
must be omitted from inserts; unchanged generated snapshot fields are ignored on
update, changed generated/ALWAYS fields fail before mutation.

The native protocol retains the SQLite transport envelope and operation names.
Atomic batches carry `actions:[{kind:"insert",row:{...}},
{kind:"update",original:{...},row:{...}},
{kind:"delete",original:{...}}]` for one table. Counts are returned. Each
existing action requires complete original non-NULL PKs, including composite keys.
Actual rows are locked before writes; supplied original fields are compared using
their PostgreSQL types. Stale/missing/nonunique rows, unknown columns, invalid
values and duplicate actual identities fail. Every row action must affect exactly
one row; constraints, trigger-suppressed writes and later errors roll back the
whole transaction. Unchanged updates verify/lock one row and count one without
issuing an unnecessary UPDATE. Inserts do not require a PK.

## Typed JSON and arrays

Bindings use actual target column types, not tag shape alone. UUID, exact decimal,
date/time/timetz, timestamps, bytea and rectangular arrays use native codecs.
Numeric-looking TEXT remains TEXT. JSON objects that resemble byte/scalar tags
remain JSON. Array element codecs retain SQL NULL versus JSON null.

SQL NULL is plain null. The explicit JSON editor/input envelope is
`{"$type":"json","value":V}`; its payload is unwrapped exactly once only in
JSON/jsonb context. A literal object resembling that envelope belongs inside its
value. Native reads keep natural JSON except JSON null and literal reserved JSON
envelopes, which receive one outer envelope. UI editors wrap every actual JSON
value; exports unwrap once. Large integers remain exact.

One-dimensional JSON/JSONB arrays accept natural or explicitly enveloped JSON
elements, including nested JSON arrays. The nested array syntax is ambiguous with
multidimensional PostgreSQL JSON arrays; use one-dimensional JSON arrays for
lossless writes through this JSON protocol. General PostgreSQL array lower bounds
are not represented by the existing read wire protocol.

## Validation

Pure Rust tests cover JSON null/tag contexts, typed overflow/UUID failures,
JSONB[]/UUID[]/NUMERIC[] and timetz offsets, explicit readonly rejection before
connection, and existing SQLite/editor/identity regressions.

Opt-in real-server checks require psycopg and a private descriptor for the newly
marked disposable write fixture. They verify container ID/label, loopback alternate
port, marker database and permissions, create only owned `rust_write_test`,
and remove that schema afterward. They never accept an arbitrary URL:

```sh
python3 rust/tests/postgres_write.py --fixture /private/disposable/fixture.json \
  --worker "$PWD/rust/target/release/dbcli-rust" --family dispatch
```

No application database is mutated during tests. Historical benchmark/read-only
results describe previous artifacts and are retained separately.

## SQL export replay

Both Rust interfaces omit computed columns from SQL INSERT exports and use
`OVERRIDING SYSTEM VALUE` when exporting ALWAYS identity values. BY DEFAULT
identities retain their explicit values with ordinary INSERT. All-generated
projections use DEFAULT VALUES per row. JSON/CSV projections are unchanged.
Table definitions and identity-sequence resets are not included. See the root
[Rust guide](../RUST.md) for disposable export replay checks.
