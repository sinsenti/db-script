# Rust database browser — current SQLite and writable PostgreSQL build

Python remains the reference implementation. This branch's native Rust SQLite
browser is **writable by default**. `--read-only` opens SQLite read-only and
rejects mutations. SQLite is now **bundled with FTS5**; a system SQLite development
package or linker bootstrap is not required.

PostgreSQL now opens the same interactive browser with `--postgres`, using
`DBCLI_POSTGRES_URL` from the environment. It is writable by default; `--read-only` enforces server read-only sessions.
See [Current PostgreSQL writes and safety](POSTGRES_WRITABLE.md) for policy, codecs and protocol.

E bulk editing now opens a private Neovim diff directly. Save and exit with ` :wqa `
for a count-only confirmation; `:cq` cancels and unchanged exit never writes.
See [BULK_NEOVIM.md](BULK_NEOVIM.md). Other editor controls retain `EDITOR`.

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
Neovim diff and count-only confirmation; removing an update entry never
implies deletion. Complete original primary keys, immutable snapshots, strict
BLOB decoding and JSON TEXT preservation protect writes. Native PostgreSQL/MySQL
protocol adapters remain available. PostgreSQL interactive browsing is now available
in writable or explicit read-only mode; MySQL interactive/Docker browsing remains unsupported.

See **[WRITABLE_SQLITE.md](WRITABLE_SQLITE.md)** for authoritative current controls,
bulk/protocol formats, exports, safety rules, validation and remaining limitations.

```sh
cargo test --locked --manifest-path rust/Cargo.toml
python3 rust/tests/writable_pty.py rust/target/release/dbcli-rust --kind original
```

## Integration in main

This is the Original Rust backend selected after the PostgreSQL `quotes`
comparison. The matching Ratatui interface is in `ui/rust`. See
[the root Rust guide](../RUST.md) for a self-contained build and Docker launcher.

The comparison sources remain in their original worktrees. This merge includes
the backend and matching UI, with no dependency on those worktree paths.
