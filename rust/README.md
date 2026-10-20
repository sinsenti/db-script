# Rust database browser — current SQLite and read-only PostgreSQL build

Python remains the reference implementation. This branch's native Rust SQLite
browser is **writable by default**. `--read-only` opens SQLite read-only and
rejects mutations. SQLite is now **bundled with FTS5**; a system SQLite development
package or linker bootstrap is not required.

PostgreSQL now opens the same interactive browser with `--postgres`, using
`DBCLI_POSTGRES_URL` from the environment. It is always server-enforced read-only.
See [PostgreSQL browsing](POSTGRES_READONLY.md) for policy, codecs and protocol.

## Current quickstart

From the repository root, in a real POSIX terminal:

```sh
cargo build --release --locked --manifest-path rust/Cargo.toml
rust/target/release/dbcli-rust --db /path/to/database.sqlite --table tasks
rust/target/release/dbcli-rust --read-only --db /path/to/database.sqlite
rust/target/release/dbcli-rust --postgres --table public.items
```

Without a target, current-directory SQLite signature discovery offers a searchable
file picker. The table picker supports fuzzy search and scrolling. The browser
supports navigation and row cards, filter/sort/jump, visibility, stable marks,
CRUD, FK navigation, raw-query paging/filter/sort/export, and complete schema,
index/constraint/trigger inspection. Ctrl+G edits the browser filter **and text
inside active `/` or `:` prompts**; Tab cycles contextual completion suggestions.

Multi-insert, explicit bulk editing and marked `dd` deletion use atomic SQLite
transactions. `E` uses `{updates:[{original,row}], delete:[], insert:[]}` with a
complete scrollable preview and confirmation; removing an update entry never
implies deletion. Complete original primary keys, immutable snapshots, strict
BLOB decoding and JSON TEXT preservation protect writes. Native PostgreSQL/MySQL
protocol adapters remain available. PostgreSQL interactive browsing is now available
in forced read-only mode; MySQL interactive/Docker browsing remains unsupported.

See **[WRITABLE_SQLITE.md](WRITABLE_SQLITE.md)** for authoritative current controls,
bulk/protocol formats, exports, safety rules, validation and remaining limitations.

```sh
cargo test --locked --manifest-path rust/Cargo.toml
python3 rust/tests/writable_pty.py rust/target/release/dbcli-rust --kind original
```

## Historical baseline notes — not current behavior

**Everything below describes the earlier frozen benchmark implementation.** It is
retained for historical build/measurement context. Its system-SQLite requirements,
nontransactional bulk workflow and claims about missing discovery/completion or
active-prompt Ctrl+G are obsolete. Use the current quickstart and linked document
above for this branch; the historical measurements have not been rerun or relabeled.

This is the Rust CLI/browser port for SQLite plus the selected PostgreSQL/MySQL
JSONL workloads. Python remains the reference implementation and full
multi-engine application. `rusqlite` links the system SQLite library;
`--version` prints its version so comparisons can disclose engine-version
differences. The SQLite path uses short-lived connections, bound mutation
values, original/composite-primary-key identity checks, and bounded read-query
handling. PostgreSQL/MySQL support is currently protocol-only; the interactive
browser and Docker/legacy CLI discovery paths remain SQLite-only.

### Build and launch

A SQLite development library/linker file is required to build (for example,
`libsqlite3-dev` on Debian/Ubuntu); the executable dynamically links the system
SQLite runtime (`libsqlite3.so.0` on Linux). Keep the runtime version visible
with `--version` when comparing candidates.

From the repository root:

```sh
cd rust
cargo build --release
./target/release/dbcli-rust --help
./target/release/dbcli-rust path/to/database.sqlite
./target/release/dbcli-rust --read-only path/to/database.sqlite
./target/release/dbcli-rust --table people path/to/database.sqlite
```

Noninteractive helpers:

```sh
./rust/target/release/dbcli-rust --list-tables path/to/database.sqlite
./rust/target/release/dbcli-rust --schema people path/to/database.sqlite
./rust/target/release/dbcli-rust --query 'SELECT id, name FROM people' --limit 50 path/to/database.sqlite
```

`--query` accepts only `SELECT`, `WITH`, and `EXPLAIN` through the same
bounded read-only API used by the browser. Its checks are not a sandbox for
untrusted SQL or side-effecting functions; do not use it as an isolation
boundary. Use database permissions for a hard read-only guarantee.

The interactive POSIX SQLite TUI supports `j/k`, `H/L`, `gg/G`, `Ctrl-D/U`,
row cards (`Enter/l`), table filters (`/`), expression/query/jump prompt (`:`),
sorting (`o/O`), column visibility (`v`), row marks (`Space`), foreign-key
navigation (`f`), schema/index views (`S/I`), insertion/edit/bulk-edit/delete
(`a/e/E/dd`), and JSON/CSV/SQL exports (`x/X`). JSON editing uses `$EDITOR`
then `nvim`, `vim`, or `nano`; temporary editor files are removed on success
and failure. The TUI requires a POSIX terminal. `E` edits up to 100,000 filtered/sorted
rows, matches them by their unchanged primary-key values, previews the proposed
changes and asks before applying; omitted rows are deletions and the sequence is
not transactional. It refuses bulk edits without a primary key and directs users
to single-row editing for primary-key changes. Writes remain per-row commits,
like the Python adapter/UI path.

### Compatibility notes

- Pass a SQLite path explicitly. Unlike Python, this port does not discover
  SQLite files in the current directory and does not discover Docker databases.
- The Rust UI displays the full schema table (the current Python `S` screen only
  shows its first schema row). The `I` inspector reports indexes, foreign keys
  and triggers in a compact table rather than Python's decorated text layout.
- Prompt history, SQL autocomplete and `Ctrl+G` editor handoff for the filter
  prompt are not implemented. `$EDITOR` handoff is implemented for row and
  table JSON editing.
- PostgreSQL/MySQL adapters are exposed only through the shared JSONL protocol,
  not through the interactive browser. For safety, protocol URLs are restricted
  to the approved localhost Compose fixtures; this is not a general production
  remote connection interface. Python remains the supported multi-engine
  application.

The persistent JSON-lines adapter/render protocol used by the shared benchmark
harness is available with:

```sh
./rust/target/release/dbcli-rust --protocol --db /path/to/copied.sqlite
```

The selected remote workloads use the same protocol without a database path;
credentials are read only from the engine-specific environment variable and
must identify the approved disposable Compose service:

```sh
./rust/target/release/dbcli-rust --protocol --engine postgres
./rust/target/release/dbcli-rust --protocol --engine mysql
```

Set `DBCLI_POSTGRES_URL` / `DBCLI_MYSQL_URL` in the environment. The protocol
rejects endpoints other than the fixed localhost Compose host, port, database,
and fixture credentials. It reads one request per stdin line, writes one JSON
response per stdout line, and emits diagnostics only to stderr.

Remote parity resets and drops the fixed fixture tables in both services. Use
only the disposable Compose endpoints and coordinate an exclusive fixture slot
before running it; remote timing additionally requires three-candidate parity
and the shared timing lock. See [`REMOTE.md`](../benchmarks/language_comparison/REMOTE.md)
for the exact shared protocol and commands.

### Tests and parity

```sh
cd rust
cargo test
cargo build --release
cd ..
python3 benchmarks/language_comparison/parity.py --rust-bin rust/target/release/dbcli-rust
```

The shared harness creates disposable fixtures and checks ordered result
values, blobs/NULLs, schema and FK metadata, SQL/bind traces, CRUD state,
read-only and raw-query rejection cases, and fixed-dimension rendering against
the production Python adapter/UI. Never run mutation workloads against a
personal or production database.
