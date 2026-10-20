# Ratatui database browser

A native Rust UI over the existing native worker executables. It runs without
Python. The selected Original Rust backend lives in `rust/`. See [RUST.md](../../RUST.md)
for the main-worktree launcher; previous comparison builds remain preserved.

## Build and launch

Rust 1.98.1 was used for this build. Ratatui is pinned to 0.30.2; the frontend uses
Ratatui's crossterm re-export (0.29), avoiding a second event/backend version.

```sh
./ui/rust/build.sh
./ui/rust/build.sh test       # focused tests, then optimized build
ui/rust/target/release/dbcli-ui-rust --help
ui/rust/target/release/dbcli-ui-rust \
  --engine sqlite --database /path/to/disposable.sqlite --table items \
  --backend original-rust --worker /absolute/path/to/dbcli-rust
```

Set PostgreSQL credentials securely in `DBCLI_POSTGRES_URL`, then use:

```sh
ui/rust/target/release/dbcli-ui-rust --engine postgres --table public.items \
  --backend original-rust --worker /absolute/path/to/dbcli-rust
```

PostgreSQL credentials are never accepted as frontend arguments. Compatible worker
environment aliases are inherited. SQLite and PostgreSQL are writable by default;
`--read-only` disables mutations in the UI and workers. Use only trusted worker executables.

`--backend` supports `original-rust`, `codex-rust`, `pi-rust`, `integrated-rust`
and the corresponding four `-go` aliases. `--worker` selects the executable; the
alias selects its protocol. The root integration launcher resolves worker paths
and authorized Docker environments. There is no Python fallback.

## Interface and controls

The screen has an engine/backend/read-only header, searchable table sidebar,
natural-width horizontally scrolling grid, Data/SQL/Schema/Inspect views, status
and help footer. Resize is supported. Unicode/grapheme widths are used for text
windows; Ratatui handles terminal cell widths. Database reads and writes never run
inside rendering. Busy/error states are visible, and quit remains responsive
while a worker is waiting on a read or protocol response. During a pending write,
q waits for the transaction result; forced Ctrl+C/signal exit reports an unknown
outcome rather than claiming rollback.

| Control | Action |
| --- | --- |
| Tab | Cycle grid, view tabs, sidebar focus |
| j/k, arrows | Row, sidebar, chooser or inspector navigation |
| H/L, left/right | Column focus with horizontal scrolling |
| Enter/l | Row detail; Enter chooses sidebar/format/FK item |
| h/q, Esc | Back/quit; cancel a modal without applying it |
| gg/G, Ctrl+D/U | First/last row and paging |
| / | Filter data; search tables when sidebar focused |
| : or 2 | Open multiline SQL editor |
| Ctrl+R | Run SQL, or parse JSON and show mutation preview |
| Ctrl+G | Open active prompt/editor in `$EDITOR` |
| o/O | Toggle focused column ascending/descending sort |
| v | Column visibility chooser; last entry restores all |
| Space | Toggle complete-row marks, stable across pages/sort |
| f | FK chooser and nested browser, including composite/cross-schema FKs |
| S/3, I/4 | Full schema and index/constraint/trigger views |
| x, X | JSON export; JSON/CSV/SQL format chooser |
| a/e | Insert or edit one original row |
| E | Direct Neovim bulk diff, then count-only confirmation |
| dd | Delete current or marked rows after full preview |
| ? | Help modal |
| Ctrl+P | Discoverable filtered command palette |

Text modals support Unicode character insertion, arrows, Home/End, Ctrl+A/E,
Ctrl+K to delete to line end, Ctrl+U to clear, Backspace/Delete and multiline
up/down. Tab completes context columns/tables/keywords, or palette commands.
Enter inserts a newline in SQL/JSON; Ctrl+R runs/previews. Enter accepts a filter,
export path, sidebar search or command. A positive row number in the SQL editor
jumps within the current result. The SQL draft is retained when its modal closes.

`EDITOR` is parsed as program plus arguments, without shell evaluation. Without
it, nvim/vim/nano are tried. Temporary files are mode 0600 and removed after
success, failure or handled cancellation. The terminal is restored before editor
handoff and on normal exit, errors, panic and handled INT/TERM/HUP.

## Neovim bulk editor

Only E uses DBCLI_BULK_EDITOR, default nvim. Executable/quoted arguments are parsed
without a shell; nvim -u NONE is useful for reproducible tests. Ordinary Ctrl+G
uses EDITOR as before. Standard diff/privacy arguments precede the two private
original.json and edited.json paths (edited last, focused). User configuration is
otherwise honored; modelines/exrc, swap, backup/writebackup, persistent undo and
shada are disabled for this session. Original-file bytes are verified unchanged.
Directories are 0700 and files 0600, removed on success/error/cancel/handled signals.
Pre-editor termios and descriptor flags are restored, and editor children reaped.

Real disposable check (requires installed Neovim):

```sh
python3 ui/rust/tests/bulk_neovim_pty.py ui/rust/target/release/dbcli-ui-rust \
  --kind ratatui --worker /absolute/path/to/dbcli-rust
```

This runs actual Neovim no-op/:cq/edit/signal flows against only temporary SQLite,
verifying buffer protection, no-op/cancel DB bytes, confirmed writes, untouched
JSON TEXT/BLOB/leading-zero fields, cleanup and terminal restoration.

## Mutation safety

SQLite/PostgreSQL insert accepts one JSON object or an array of objects. Existing JSON TEXT
objects/arrays are structured for editing and normalized back before binding;
leading-zero TEXT and genuine BLOB values retain their types. JSON objects that
resemble byte tags remain TEXT when they originated as structured JSON TEXT.

Edits retain the complete immutable original row and all original PK columns.
Unknown columns, missing/NULL identity and changed original snapshots are rejected.
No-PK tables remain browsable, exportable and insertable, with an actionable error
for edit/delete/bulk. No-PK/raw result marks use full content identity, so identical
duplicate rows coalesce into one marked snapshot. Raw result screens disable mutations. Both the UI and bridge reject writes in read-only mode.

PostgreSQL uses the upgraded writable worker releases. Generated/default/identity
columns are omitted from insert templates; users may add optional default fields.
The worker locks complete original PKs, rejects stale snapshots/generated changes,
checks exactly-one counts and rolls back an entire batch on any failed action.
Every read/raw operation remains a verified server read-only transaction, separate
from writable mutation connections, with UTC and bounded connection/statement/lock
waits. No automatic write retry occurs after an uncertain COMMIT.

PG JSON editors use the explicit outer `{"$type":"json","value":V}` envelope;
SQLNULL is plain null, JSONnull is an envelope with null payload, and tag-like JSON
objects belong inside its value. Payloads are unwrapped exactly once, with no
recursive byte/scalar conversion. JSON exports show natural payloads; SQL exports
preserve SQLNULL flags and actual JSON scalar types. Native UUID/decimal/date/time/
bytea/array values retain worker codecs. One-dimensional JSONB[] elements are
context-enveloped so literal byte tags and nested JSON arrays remain JSON.

Bulk editing uses:

```json
{"updates":[{"original":{"id":1,"name":"old"},"row":{"id":1,"name":"new"}}],"insert":[],"delete":[]}
```

`original` must remain the full source snapshot, including all non-PK columns.
`inserts` is also accepted as an alias for `insert`. Omitted updates do not imply
deletion. Duplicate identities are rejected, and PK changes use the single-row
editor. E opens a private Neovim diff directly: original plan readonly/nomodifiable on
the left, edited plan focused on the right. Edit updates[].row, insert[], or
explicit delete[]. :wqa saves/exits to a small count-only TUI confirmation; y
applies one atomic transaction. :cq/nonzero exit cancels. Unchanged exit and
unchanged update entries never dispatch writes. No full-row TUI preview is built
for E. Plans containing deletes require DELETE then y; Esc/q cancels confirmation.
Other mutation actions retain their normal full previews.
All actions go through the worker's atomic `apply_batch`, including multi-insert
and marked deletion. Worker constraints, stale-row or affected-count failures roll
back its batch. If the UI is terminated after confirmation, a write may already
have committed; cancellation does not promise rollback of completed work.

## Reads, metadata and exports

Filters and sort use the existing worker semantics. Plain-text generated values
are bound; identifiers are separately quoted. FK predicates bind every paired
value, preserve constraint grouping and schema, and remain active alongside nested
filters. Metadata is fetched in background jobs and cached by table, never per row.

SQL runs through the worker's bounded read-only SELECT/WITH/EXPLAIN path. Result
browsers support pagination/filter/sort/detail/visibility/marks/export. EXPLAIN can
be paged but cannot be filtered/sorted as a subquery. SELECT result counts use the worker read path; EXPLAIN totals are bounded to
100,000 rows because EXPLAIN cannot be counted as a subquery. Raw result SQL exports
use `query_result` as a placeholder destination.

Unmarked export and bulk selection fetch up to 100,000 rows in current filter/sort
order. Marked export uses full selected snapshots, including hidden columns.
JSON exports preserve ordinary JSON-looking TEXT as a string and native PostgreSQL
JSON as structured/scalar JSON. BLOBs use `{"$bytes":"hex"}`. UUID/decimal/date/time/
datetime retain worker typed envelopes. CSV uses scalar display text and cannot
encode all database storage types by itself. SQL uses quoted identifiers, exact
literals, SQLite hex/PG bytea expressions and PostgreSQL array text, including empty
arrays. JSON column type guides scalar JSON SQL literals; a bounded read projection
captures SQL-NULL flags in the same row as the actual typed JSON values, retaining
the difference between JSON `null` and SQL NULL even in marked exports. Auxiliary
aliases cannot collide with actual result headers and are stripped from data.

Metadata, data and count requests are short-lived worker operations. Concurrent
external changes can therefore change a page/count between requests. Atomic writes
still validate original snapshots in the worker. Large bulk plans/exports are held
in memory; the cap is not a streaming or unlimited export guarantee.

The frontend adds no database driver and inherits each worker's backend scope and
query policy. PostgreSQL validation here used only the authorized disposable local
PostgreSQL 16 fixture over NoTLS. TLS-required endpoints and unsupported worker
native types remain unsupported. Read-only SQL heuristics are not an untrusted
function sandbox; server-enforced read-only transactions belong to the selected
PostgreSQL worker. Persistent prompt history, clipboard and mouse control are not
part of this contract. Completion is contextual, not a complete SQL grammar.

## Architecture

- `bridge.rs`: dispatch, batch and rewrite JSONL envelopes; mutation/byte-tag
  translation; structured errors; independent owned-child cancellation/reaping.
- `data.rs`: background request queue, cached PK/schema/FK metadata, bounded result
  composition, typed values, atomic plan validation and JSON/CSV/SQL exports.
- `app.rs`: event-driven state, nested browser stack, modals, drafts, marks and
  confirmation. Raw snapshots remain separate from editable JSON.
- `view.rs`: pure Ratatui widgets, responsive column viewport and Unicode text.
- `main.rs`: CLI, event polling, signal/terminal lifecycle and editor handoff.

Quit/drop kills and waits for the owned worker through its `Child` handle, without
locking the request reader. This also covers a blocked startup handshake and an
exit before startup completes; no PID-only signaling of unrelated processes is used.

## Validation

```sh
cargo test --locked --manifest-path ui/rust/Cargo.toml
DBCLI_RUN_INTEGRATION=0 python3 -m unittest discover -s tests -q
```

Real worker checks are opt-in and mutate only a new temporary on-disk SQLite
fixture, with FTS5, BLOB/JSON/TEXT and composite PK/FK coverage:

```sh
export DBCLI_UI_TEST_WORKERS='{"original-rust":"/absolute/path/to/dbcli-rust"}'
cargo test --locked --manifest-path ui/rust/Cargo.toml --test workers
```

To include PostgreSQL reads, set `DBCLI_UI_TEST_POSTGRES_FIXTURE` to the authorized
private disposable read or write descriptor (marker/database/permissions/container
ID and dedicated fixture label are checked). The test
inherits its URL in memory, checks actual/default read-only settings, reads rich
rows, generates typed SQL and checks composite FKs. It never writes PostgreSQL.
Tests without the variables report those checks as skips on captured test output.
The blocked-worker lifecycle test always runs and verifies kill/reap independently
of a response read. The standalone PostgreSQL checks below use the marked fixture managed by
`tools/postgres_write_fixture.py` and create their own schema; they require no
benchmark tables. The optional Rust read-fixture test additionally expects the
older rich fixture dataset. No benchmark harness is imported as production code.

Implementation references: [Ratatui JSON editor](https://ratatui.rs/tutorials/json-editor/)
and [external editor handoff](https://ratatui.rs/recipes/apps/spawn-vim/).


### Disposable PostgreSQL Neovim check

With psycopg installed in a development interpreter:

```sh
python3 ui/rust/tests/bulk_postgres_pty.py ui/rust/target/release/dbcli-ui-rust --kind ratatui \
  --fixture /private/disposable/postgres-write/fixture.json --worker /absolute/path/to/dbcli-rust
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
