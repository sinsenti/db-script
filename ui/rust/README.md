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

New database sessions start with focus on the table sidebar. Use j/k to choose a
table and Enter/l to open its rows. Tab (or Shift+Tab) switches only between the
sidebar and row pane; the view bar is not a focus stop. From the row pane, 1/2/3/4
still selects Data/SQL/Schema/Inspect.

`?` opens a sectioned keyboard guide with one shortcut or related key group per
line, aligned descriptions and a scroll position indicator. Use j/k, Ctrl+D/U or
g/G to move through it; Esc/q closes it.

Schema and inspector results use aligned, Unicode-aware table columns, alternating
row shading and a fixed header. Long values wrap inside their own cells. Use j/k
to scroll entries, Ctrl+D/U to page, and g/G to jump to the first/last entry.

| Control | Action |
| --- | --- |
| Tab / Shift+Tab | Switch between table sidebar and table rows |
| j/k, arrows | Row, sidebar, chooser or inspector navigation |
| H/L, left/right | Column focus with horizontal scrolling |
| Enter/l | Row detail; Enter chooses sidebar/format/FK item |
| h/q | Back/quit |
| Esc | Close modal; otherwise cancel pending read without exiting, or back when idle |
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
| a | Insert a row using the built-in JSON prompt |
| e | Open the current row directly in the external editor, then review changes |
| E | Direct Neovim bulk diff, then count-only confirmation |
| dd | Delete current or marked rows after full preview |
| ? | Help modal |
| Ctrl+P | Discoverable filtered command palette |

Text modals support Unicode character insertion, arrows, Home/End, Ctrl+A/E,
Ctrl+W to delete the preceding whitespace-delimited word (including whitespace
immediately before the cursor), Ctrl+K to delete to line end, Ctrl+U to clear,
Backspace/Delete and multiline
up/down. Tab completes context columns/tables/keywords, or palette commands.
Enter inserts a newline in SQL/JSON; Ctrl+R runs/previews. Enter accepts a filter,
export path, sidebar search or command. A positive row number in the SQL editor
jumps within the current result. The SQL draft is retained when its modal closes.

## Filter suggestions

With the grid focused, `/` opens a filter prompt with a suggestion list. Type a
column prefix, use ↑/↓ (or Shift+Tab to move backward), and press Tab to accept.
The next suggestions offer predicates such as `=`, `LIKE`, `IN (` and `IS NULL`;
PostgreSQL also offers `ILIKE`. After an operator, short scalar values from the
already-loaded page can be suggested with SQL quoting. Column names are quoted
as identifiers. For example, type `ta`, accept `tags`, accept `=`, then choose a
cached tag value or type your own quoted string. Single-word plain searches also
suggest short cached text, without adding SQL quoting or changing search mode.

Enter always submits the filter as typed; it does not accept the highlighted
suggestion. Esc first hides suggestions, then closes the prompt. Tab reopens a
hidden list. Cursor-local completion replaces the current token while preserving
surrounding clauses; Ctrl+W and other text editing shortcuts still work. Typing
without accepting a suggestion retains the existing plain-text search / SQL
predicate behavior. Sidebar `/` continues to search table names separately.

Suggestions use metadata and at most the first 50 loaded rows, with at most 40
items. Scalar value suggestions are short values only. They do not fetch more rows, scan the table,
or run SQL while typing/rendering. Values missing from the current page must be
typed manually. This is contextual completion, not a complete SQL parser.

### JSON keys and paths

The same suggestion list includes JSON paths discovered from the loaded page.
Type a JSON column followed by `.`, such as `extended_properties_json.`, select
an observed nested key or array path, and press Tab. Quoted column prefixes such
as `"payload".` also work. A path like `payload.customer.name` or
`payload.items[0].code` is a completion label; Tab replaces it with executable SQL.
The shorthand itself is not a new filter language: accept the suggestion before
submitting. The next suggestions offer predicates and observed scalar values for
that specific path. Surrounding filter clauses and cursor-local editing remain
supported.

SQLite JSON stored as TEXT uses a guarded `json_extract` expression, so malformed
JSON in other rows is ignored by that extraction. PostgreSQL native JSON/JSONB
uses `#>> ARRAY[...]` with quoted keys and array indexes. This extracts **text**:
its numeric/boolean value suggestions are quoted text, and numeric comparisons
require an explicit cast. A missing path and a JSON null both extract SQL NULL;
use `IS NULL`, or write a type/existence expression to distinguish them. SQL syntax:
[SQLite JSON functions](https://www.sqlite.org/json1.html) and
[PostgreSQL JSON operators](https://www.postgresql.org/docs/16/functions-json.html).

Discovery is cached when a page loads, not repeated on each keystroke. It samples
at most 50 rows / 64 columns, five path segments, 32 children per object and the
first eight elements per array, retaining at most 128 paths and 12 short values
per path. SQLite parsing skips documents over 64 KiB and stops after 256 KiB of
JSON text; traversal has a 2,048-node budget. Empty/unseen keys cannot be suggested.
SQLite keys containing double quotes, backslashes or control characters are not
offered because their path escaping is ambiguous; enter a suitable expression
manually. PostgreSQL TEXT and native JSON-array columns are outside native
JSON/JSONB path discovery. No additional database reads are issued for discovery.

## Direct single-row editing

Lowercase `e` opens the current writable table row directly in `$EDITOR` (falling
back to nvim, vim, then nano). Edit the JSON and save/exit; the browser parses it
and displays the existing explicit mutation review. Press `y` to apply. Editor
exit alone never writes to the database. An unchanged document, including JSON
whitespace-only changes, performs no writes. A nonzero editor exit cancels.

The immutable original row/PK remains in memory for the existing stale-snapshot
and affected-row checks. Untouched fields keep their exact original storage
values, including JSON TEXT formatting. Invalid edited JSON stays in the built-in
prompt for correction with Ctrl+G and Ctrl+R. Raw SQL results and read-only table
sessions still reject editing. Uppercase `E` retains its separate bulk Neovim diff
workflow. Inserts keep their built-in JSON prompt; Ctrl+G still opens that prompt
in an external editor.

`EDITOR` is parsed as program plus arguments, without shell evaluation. Without
it, nvim/vim/nano are tried. Temporary files are mode 0600 and removed after
success, failure or handled cancellation. The terminal is restored before editor
handoff and on normal exit, errors, panic and handled INT/TERM/HUP.

## Worker I/O and read cancellation

While a read is pending, Esc stops it and keeps the browser open. A modal consumes
the first Esc. New filter/SQL submissions and sidebar table selections supersede
older reads. The background queue skips obsolete queued work, checks cancellation
between pipe polls, and stamps each completion with a request ID. The UI checks
that ID before changing any state, including errors and editor/export handoffs.
Confirmed writes stay gated until the UI receives their result; read cancellation
and new submissions cannot interrupt them.

A cancelled, oversized, malformed or timed-out exchange discards and reaps its
worker. The next request starts a fresh process with fresh metadata caches. No
operation is retried automatically. For Original Rust/PostgreSQL reads, a private
inherited Unix socket carries the request ID to the worker, which attempts driver
CancelRequest using its current read connection's token. The UI gives this attempt
a 350 ms absolute grace budget, then kills/reaps the worker. PostgreSQL provides
no cancellation-success acknowledgment; disconnect detection and server timeouts
remain fallbacks. Stale IDs cannot cancel newer reads, and writable connections
never register read-cancellation tokens. Other engines/backends use worker discard.
Ctrl+P → `refresh` retries the current view or cancelled initial discovery.

Default request/response caps are **128 MiB each**, excluding JSONL's newline;
the total write/read I/O deadline is **60 seconds per exchange**, including
blocked pipe writes. Startup ready handshakes have the same deadline. Partial
response progress does not extend it. Configure positive
`DBCLI_WORKER_MAX_REQUEST_BYTES` / `DBCLI_WORKER_MAX_RESPONSE_BYTES` (up to 1 GiB)
and `DBCLI_WORKER_DEADLINE_MS` (up to 600000 ms) in the environment. Invalid values
are rejected before worker startup. These cap serialized frames, not backend
allocations, parsed JSON memory or the total multi-request job duration.

Writes use the same I/O deadline. If their confirmation is lost, the UI keeps the
existing unconfirmed-outcome warning; it never claims rollback or retries the
write. Refresh/inspect before any retry. PostgreSQL query/lock timeouts and
transactional snapshot checks remain separate. See [RUST.md](../../RUST.md).

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
{"updates":[{"original":{"id":1},"row":{"id":1,"name":"new"}}],"insert":[],"delete":[]}
```

In the Neovim files, `original` must remain the unchanged complete primary-key
object only; composite keys include every key field. Keep full editable values
under `row`. The UI retains full original snapshots in memory and restores them
only for requested actions before the existing validation/atomic worker call.
Explicit `delete` entries use the same key-only objects. Worker protocol mutation
requests still carry complete original snapshots, including non-PK columns.
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
All formats create a new private 0600 destination exclusively. Existing files,
directories and symlinks are refused; the path prompt stays open so you can choose
another filename. A failed export
removes its own partial file rather than leaving a truncated result.
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
  translation; capped nonblocking pipe I/O, absolute deadlines, owned-child reaping.
- `data.rs`: request generations, cancellation, gated writes, cached PK/schema/FK metadata, bounded result
  composition, typed values, atomic plan validation and JSON/CSV/SQL exports.
- `app.rs`: event-driven state, nested browser stack, modals, drafts, marks and
  confirmation. Raw snapshots remain separate from editable JSON.
- `json_completion.rs`: bounded nested-key/array discovery when a page loads,
  engine-specific JSON path expressions and cached scalar values.
- `filter_completion.rs`: cursor-local token completion from cached metadata/rows,
  bounded suggestions and identifier/value quoting; no database access.
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

Both diff files carry the compact explicit plan: immutable PK tokens plus full
editable rows. Source snapshots stay in memory for validation, preserving stale-row
checks without duplicating every original non-key field in the files. Bulk plans
remain bounded in-memory operations; this does not imply streaming or a measured
process-memory improvement. Private row data is not logged.

### PostgreSQL SQL replay

SQL INSERT exports omit computed/generated columns. Exported ALWAYS identity
values use `OVERRIDING SYSTEM VALUE`; BY DEFAULT identities use ordinary INSERT.
An export with no insertable projected columns uses one `DEFAULT VALUES` statement
per row. JSON/CSV continue to include generated columns. Exports do not create the
destination table or reset identity sequences. See [RUST.md](../../RUST.md) for
opt-in disposable replay tests and the standalone performance-tool commands.
