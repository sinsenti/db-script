# Neovim bulk editing

Press E on a writable table screen. The browser fetches the filtered/sorted rows
or uses marked snapshots, validates complete original PKs, and opens one native
Neovim diff directly. There is no intermediate JSON TUI editor and no full-row
TUI diff after exit.

- Left: private original plan, readonly and nomodifiable.
- Right: editable plan, focused initially. Edit updates[].row, insert[], or explicit
  delete[]. Original snapshots and complete PKs remain immutable.
- :wqa saves and exits. Only update/insert/delete counts appear in the TUI;
  y + Enter confirms one atomic transaction. Explicit deletes first require DELETE.
- :cq or any nonzero exit cancels. An unchanged exit displays No changes — no writes.
  Unchanged update entries are removed before normalization, so changing one row
  cannot rewrite untouched JSON TEXT rows.
- Read-only sessions and raw-query screens disable E. All existing PK, stale,
  typed-value, generated-column and transaction rollback checks remain in workers.

Only E uses DBCLI_BULK_EDITOR, default nvim. It accepts an executable and quoted
arguments parsed without a shell; for example, nvim -u NONE for isolated testing.
Ordinary single-row/filter/SQL editor handoff retains EDITOR.

The command receives standard Neovim diff/privacy arguments followed by the private
original.json and edited.json paths (edited last). Wrapper overrides should exec
Neovim with those arguments. User configuration is otherwise honored, while
modelines/exrc, swap, backup, writebackup, persistent undo and shada are disabled
for this session. Original-file bytes are verified unchanged after successful exit.

The directory is 0700 and both files 0600. Owned files/directories are removed on
success, editor error, cancellation and handled signals. The editor child is
reaped; pre-editor termios and descriptor flags are restored before returning to
the browser. Plans remain bounded to 100000 rows and are held in memory; this is
not streaming bulk editing. No automatic retry follows an uncertain database write.

## Validation

```sh
cargo test --locked --manifest-path rust/Cargo.toml
cargo build --release --locked --manifest-path rust/Cargo.toml
python3 rust/tests/bulk_neovim_pty.py rust/target/release/dbcli-rust --kind original
DBCLI_RUN_INTEGRATION=0 python3 -m unittest discover -s tests -q
```

The real-Neovim PTY checks use only synthetic temporary on-disk SQLite: unchanged
exit, modified :cq, confirmed edit and SIGTERM while editing. They verify buffer
protection/focus/privacy settings, 0700/0600 permissions, no DB changes for no-op or
cancel, untouched row storage, cleanup and exact terminal restoration. Native
runtime code has no Python dependency; Python is only a developer test runner.


### Disposable PostgreSQL Neovim check

With psycopg installed in a development interpreter:

```sh
python3 rust/tests/bulk_postgres_pty.py rust/target/release/dbcli-rust --kind original \
  --fixture /private/disposable/postgres-write/fixture.json
```

This strictly validates the dedicated fixture marker/database, alternate loopback
endpoint, container ID and fixture label. It creates/removes only owned
rust_quotes_test, testing real Neovim no-op/cancel/edit/signal paths with native
JSON/JSONB, SQLNULL versus JSONnull, literal tag-like JSON, large JSON integers,
bytea and leading-zero TEXT. Application databases are never mutation fixtures.

Both diff files carry a complete explicit plan, including full immutable original
snapshots and editable rows. This costs more payload/memory than a compact-PK
editor plan; no payload optimization is claimed in this phase. Source snapshots
also remain in memory for validation. Private row data is not logged.
