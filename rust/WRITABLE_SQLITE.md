# Writable SQLite browser upgrade

This branch keeps the original Rust implementation and native remote drivers. SQLite now builds from the locked bundled SQLite source, with FTS5 enabled, so virtual-table triggers do not depend on the host SQLite installation. Earlier language-comparison measurements describe the earlier frozen build, not this upgraded binary.

## Build and run

```sh
cargo build --release --locked --manifest-path rust/Cargo.toml
rust/target/release/dbcli-rust --db /path/to/database.sqlite --table tasks
rust/target/release/dbcli-rust --read-only --db /path/to/database.sqlite
```

No SQLite header/linker bootstrap is needed. With no database argument, the browser offers current-directory files with a SQLite signature. The table menu supports `/` search and scrolling. The browser is writable by default; `--read-only` opens SQLite read-only, hides write actions, and rejects adapter writes.

## Interaction

- `j/k`, `H/L`, Enter/l row detail, `gg/G`, Ctrl+D/U, `:` row number, `h/q/b` back.
- `/` filters; `:` accepts SELECT/WITH/EXPLAIN or predicates. Ctrl+G edits current filter or active prompt text in `$EDITOR`. Tab cycles contextual columns, tables, keywords, scalar values, JSON paths/values and FK JOIN ON suggestions; matching is prefix or fuzzy. Prompts retain editable initial text; Ctrl+U clears it.
- `o/O` toggles ascending/descending sort on the focused column. `v` chooses columns to hide/restore. Space marks complete rows by primary-key identity; marks remain stable across paging and sort changes.
- `a` accepts one JSON object or an array of objects and inserts atomically. `e` edits a row, including from row detail. `dd` requires confirmation; marked rows are deleted together in one atomic transaction after a complete scrollable identity preview. Original complete composite primary keys identify writes even when keys are edited.
- `E` edits the filtered/sorted rows or marked selection using `{updates:[{original,row}], delete:[], insert:[]}`. Original snapshots are immutable. Missing update entries never imply deletion. Preview every proposed action with j/k, Ctrl+D/U, gg/G; q finishes preview. Explicit deletions additionally require `DELETE`, then y confirms the atomic transaction. Duplicate identities, stale snapshots, unknown columns, incomplete/NULL keys, constraints and unexpected affected counts roll back the entire batch.
- `f` offers an FK chooser and a nested browser. Implicit parent columns resolve against ordered parent primary keys for navigation; public metadata retains Python's `None` string for compatibility.
- Raw queries open a nested read-only result browser with paging, filter, sort, detail, marks and export. EXPLAIN supports paging but cannot be filtered/sorted as a SELECT subquery.
- `S` pages complete schema metadata; `I` pages indexes, constraints and triggers.
- `x` exports JSON; `X` selects JSON/CSV/SQL. Without marks, export is limited to 100,000 rows in the active filter/sort order. Marked exports include the full selected snapshots. BLOBs use `{$blob:base64}` JSON tags and SQL hex literals. JSON object/array TEXT is structured in editors/JSON exports; leading-zero TEXT stays a string. Existing JSON TEXT resembling a BLOB tag retains TEXT storage through editing. CSV cannot carry SQLite storage types by itself.

The SQLite raw-query authorizer and predicate classifier are not an untrusted-SQL sandbox. Primary-key-less tables remain browsable/exportable/insertable, but row edit/delete/bulk actions refuse ambiguous identity. Raw duplicate rows without primary keys use content identity for marks. PostgreSQL interactive browsing is now available as a separate always-read-only mode; see [POSTGRES_READONLY.md](POSTGRES_READONLY.md). MySQL remains a native protocol backend. Completion is bounded and contextual, not Python prompt-toolkit's full grammar-aware interface. Unicode display uses character counts, so combining/wide characters remain a rendering limitation.

## Validation

```sh
cargo test --locked --manifest-path rust/Cargo.toml
python3 rust/tests/writable_pty.py rust/target/release/dbcli-rust --kind original
DBCLI_RUN_INTEGRATION=0 python3 -m unittest discover -s tests -q
```

New regression cases cover atomic rollback including FTS triggers, composite original keys, stale snapshots, duplicate batch identities, explicit inserts/deletes, private editor files, JSON tag-like TEXT and completion. The PTY runner uses synthetic on-disk SQLite only and checks inserts/edits/card edits/deletes/bulk/raw navigation, terminal restoration and FTS integrity. Independent shared acceptance and private-baseline checks are recorded by the root comparison harness; private row values are not committed here.

### Atomic protocol extension

Existing JSONL operations remain. `apply_batch` adds `table` and `actions:[{kind:"insert",row},{kind:"update",original,row},{kind:"delete",original}]`, returning an affected count. Batch writes are stricter than Python's nontransactional bulk heuristic. Public individual mutations preserve Python's no-op/missing-row counts while rolling back ambiguous changes.
