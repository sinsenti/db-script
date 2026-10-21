# Python, Go and Rust: comparison and outcome

This records the experiments that led to the current application, from the first
SQLite ports through PostgreSQL browsing, writable interfaces and Neovim bulk
editing. Results refer to the pinned historical builds below. Documentation was
assembled on 2026-10-22; no new benchmarks were run.

## What we selected

Main uses the **Original Rust backend and Ratatui frontend**. Python provides the
initial database picker and remains available as the reference browser through
`--python`; MySQL entries use Python. The normal entry command is still:

```bash
python ~/git/personal/db-script/db.py
```

The strongest evidence for selecting Original Rust came from the real PostgreSQL
`quotes` workload:

| Observed result | Original Rust | Comparison |
| --- | ---: | --- |
| Read all 771 quotes | 128.2 ms | Python UI backend: 430.8 ms; fastest Go build: 158.4 ms |
| Same supplied raw SQL, 25 rows | 10.2 ms | Python UI backend: 23.2 ms |
| Bulk worker CPU per call | 74.4 ms | Python UI backend: 357.8 ms; Go builds: 178.9–221.1 ms |
| Worker lifetime peak RSS | 53.0 MiB | Python UI backend: 82.5 MiB; Go builds: 65.1–65.4 MiB |
| Fresh worker plus SELECT 1 | 5.5 ms | Python UI backend: 125.8 ms; Codex Rust: 5.3 ms |

The full-table read was about **3.4× faster than the measured Python UI backend**.
These are end-to-end client/driver/serialization measurements, not isolated
PostgreSQL execution times. Original Rust was not fastest on every metric.

The UI comparison also exposed a substantial trade-off: Go reached Neovim sooner
and used less total memory. Updated Original Rust took **1.20 s / 369.5 MiB** from
E to editor readiness, versus Original Go **1.01 s / 200.4 MiB** and Python
Textual **1.71 s / 201.2 MiB**. Memory includes the UI, worker and editor process
tree. Low worker RSS does not establish low whole-application RSS.

See the [complete quotes results](docs/comparison/quotes-2026-10-20.md) for all
backend variants, before/after editor timings, CPU, startup, sizes and limitations.

## What we built and compared

| Family or alias | Meaning in the comparison |
| --- | --- |
| Python reference | Existing Python adapters/controller; behavior reference |
| `python` | Updated Python/Textual path with a typed safety layer |
| `original-go`, `original-rust` | Descendants of the first native ports |
| `codex-go`, `codex-rust` | Independently started direct Codex ports |
| `pi-go`, `pi-rust` | Descendants of the separate rewrite branches |
| `integrated-go`, `integrated-rust` | Pinned copies of the separate ports, not independent implementations |

The native UIs were rewritten using **Bubble Tea for Go** and **Ratatui for Rust**;
the Python alternative used **Textual**. Native frontends communicate with backend
workers over JSONL. Each language had one shared modern frontend across its
backend aliases. Later upgrades changed all families, including those with Pi
ancestry. The labels do not constitute a controlled comparison of coding agents.

## How the work progressed

The reports are successive snapshots. An early failure or missing feature is not
automatically a finding about a later release. Counts below overlap across phases;
they are not a combined current-main test total.

| Phase | Evidence and outcome |
| --- | --- |
| Controlled SQLite comparison | Python and direct Go/Rust ports passed 79 exact typed checks each before timing the 100,000-row fixture. Selected disposable PostgreSQL/MySQL checks also passed. |
| All-family audit | Broader probes found FK enforcement, integer overflow, decimal/UUID codecs, BLOBs, read-only transaction and UI gaps. At that snapshot no native port was a complete Python replacement. |
| Writable SQLite upgrades | All eight native executable choices passed 78 writable + 7 read-only adapter comparisons each, atomic/storage checks, and 264 real PTY scenarios. Disposable timetrace backups passed 122 checks per executable, including FTS triggers and integrity. |
| PostgreSQL read upgrades | 152 native typed comparisons on bounded application reads; 840 native synthetic checks. Interactive results: 147 passes, two Python rich-export limitations and four Python non-public UI skips. |
| Modern UI rewrite | 432 passing real-terminal scenarios; 72 explicitly not-applicable matrix entries. Added native inspectors, grouped foreign keys, typed exports and atomic explicit bulk plans. |
| Writable PostgreSQL | 369 shared backend assertions and 405 real PTY case/backend pairs across modern writes, read regressions and earlier interfaces. Application-container connectivity used read-only metadata; mutation tests used disposable fixtures. |
| Quotes and Neovim comparison | 10/10 backend candidates, 80 validated workloads, stable final oracle; 108/108 editor-readiness and lifecycle checks, plus 54/54 updated unsaved-diff checks. Selected Original Rust for main. |

Exact typed results, generated SQL/bindings and final database states were checked
before performance conclusions. The historical suites exposed bugs that each
implementation's own unit tests had missed. Feature parity was a gate rather than
an inference from language choice or test count.

## What the earlier SQLite experiment showed

These medians are from the direct Python/Go/Rust comparison, using SQLite 3.45.1
and 100,000 rows. They are **not measurements of the later selected main builds**.
Warm operations still opened and closed a connection for each operation.

| Workload / measurement | Python | Go | Rust |
| --- | ---: | ---: | ---: |
| Fresh process + SQLite count, wall ms | 115.614 | 3.436 | 2.416 |
| Fresh startup post-exec peak RSS, KiB | 33,404 | 11,972 | 7,404 |
| Warm page, internal ms | 0.306 | 0.305 | 0.199 |
| Warm filter/sort/count, internal ms | 64.162 | 65.804 | 65.089 |
| Warm insert/update/delete, internal ms | 9.603 | 7.351 | 9.024 |
| Warm raw query, internal ms | 2.160 | 2.165 | 2.016 |
| Warm table rendering, internal ms | 0.253 | 0.101 | 0.086 |

Compiled ports reduced process startup and memory substantially. The expensive
SQLite scan stayed around 64–66 ms in all three implementations. A rewrite did
not remove the database's query cost. Earlier cross-family scan numbers also used
different SQLite versions/build options and potentially contended workstation
runs, so their small differences cannot isolate a language effect.

Selected disposable-server workloads supported the same distinction. Medians
below include IPC, per-operation connections, authentication and driver costs:

| Engine / warm workload, wall ms | Python | Go | Rust |
| --- | ---: | ---: | ---: |
| PostgreSQL page | 14.630 | 12.220 | 12.189 |
| PostgreSQL insert/update/delete | 55.538 | 46.244 | 47.172 |
| MySQL page | 2.189 | 1.187 | 1.191 |
| MySQL insert/update/delete | 13.505 | 10.110 | 11.045 |

These used 2,000 synthetic rows on local PostgreSQL 16.14/MySQL 8.4.11 with matched
plain TCP transports. They do not measure WAN performance or establish native
interactive MySQL support in current main.

## The largest interactive improvement

For the wide quotes table, avoiding the built-in bulk JSON editor had a greater
effect on some interfaces than changing the backend language:

| Modern frontend / backend | Previous E then Ctrl+G | Direct E to Neovim diff |
| --- | ---: | ---: |
| Python / Python | 17.34 s | 1.71 s |
| Go / Original Go | 39.67 s | 1.01 s |
| Rust / Original Rust | 1.84 s | 1.20 s |

Both paths fetched the same table. The updated flow opens a private Neovim diff,
then requests count-only confirmation in the browser. Deletions are explicit;
omitting an update never implies deletion. The Rust editor added review behavior
and improved readiness, but its measured memory rose from 340.5 to 369.5 MiB.
The historical result does not support claiming an across-the-board memory win.

## What reached main after selection

| Commit | Result |
| --- | --- |
| `54f82a2` | Selected Original Rust backend (`c51eae7`) and matching Ratatui UI (`24fc85e`), with a local launcher |
| `654d994` | PostgreSQL SQL exports exclude computed columns and replay ALWAYS identities; repeatable performance tools resolve main-local paths |
| `317cb2d` | The familiar Python entry command opens database discovery and launches Rust for SQLite/PostgreSQL |
| `0eb2bf1` | Exports refuse existing destinations; Neovim files use compact primary-key tokens while full conflict snapshots remain in memory |
| `1ad177a` | Worker size caps/deadlines, read cancellation and stale-result rejection |

Current SQLite/PostgreSQL table sessions are writable by default, with original
primary-key identity, immutable snapshots and atomic batch validation. Raw SQL
screens remain read only. See [current behavior and limits](RUST.md).

**The historical performance tables predate these follow-up changes.** Compact
editor documents may reduce duplicated data, but their real quotes latency/RSS
has not been re-measured. The new Python picker also adds discovery work that was
not included in fresh-worker startup figures. Compilation of the latest worker
limits/cancellation change passed; its runtime tests were not run in that change.

## Distribution, complexity and open measurements

At the quotes snapshot, Original Rust's worker was **6.36 MiB** and its frontend
**2.80 MiB**: about **9.16 MiB** for the two executables. Original Go was
10.49 + 3.75 MiB; direct Codex Go was 6.09 + 3.75 MiB. Shared libraries, Neovim
and Python launch/runtime dependencies are additional. Direct Go used system
SQLite while Rust bundled it. These are executable footprints, not complete
portable installation sizes.

Source lines describe scope, not effort: the selected Rust worker/frontend had
8,143 / 3,848 production lines at that snapshot. No controlled developer-hour
measurement exists. Typed codecs, SQL fidelity, transactions, editor lifecycle
and terminal restoration were significant parts of the implementation work.

Remaining limits of the comparison:

- Fresh-process runs used warm server/OS caches. No OS-cache-cold or cold-server
  benchmark was performed, and the application database was never restarted.
- Quotes contained **771 wide rows × 36 columns** (2.94 MiB summed row sizes,
  4.99 MiB table storage). This is not evidence for million-row editing or streaming.
- Page endpoints and Python orchestration perform different metadata/count work.
  The supplied raw-SQL workload is the controlled query comparison.
- Backend CPU excludes PostgreSQL server work. UI RSS sums process mappings and
  can count shared pages twice. CPU ticks and sampling resolution limit precision.
- There were nine warm backend samples and three UI samples per combination.
  Small native differences, especially between copied builds, should not be ranked.
- Quotes benchmarks performed reads and cancelled editor sessions, with zero
  generated application-write attempts observed. CRUD correctness used disposable
  fixtures; application-table mutation latency was not measured.
- TLS-required PostgreSQL endpoints, all specialized types, remote networks and
  current native MySQL UI behavior are outside the established result.

## Sources and repeating the work

The [archived quotes report](docs/comparison/quotes-2026-10-20.md) keeps all result
tables and method caveats in main, without requiring the old worktrees.
Full historical reports and raw evidence remain in commit
`a44b69c56c7cbd622786e3a4ca897bee89133fcc`:

| Subject | Path in that commit |
| --- | --- |
| Direct SQLite/PostgreSQL/MySQL comparison | `benchmarks/language_comparison/REPORT.md` |
| Initial all-family findings | `benchmarks/all_implementations/REPORT.md` |
| Writable SQLite / timetrace copies | `benchmarks/writable_parity/REPORT.md` |
| PostgreSQL read parity | `benchmarks/postgres_read/REPORT.md` |
| Three modern frontends | `benchmarks/ui_v2/REPORT.md` |
| Writable PostgreSQL | `benchmarks/postgres_write/REPORT.md` |
| Quotes / Neovim performance | `QUOTES_PERFORMANCE.md`, `benchmarks/quotes_perf/` |

For example, inspect a historical report without switching branches:

```bash
git show a44b69c:benchmarks/language_comparison/REPORT.md
git show a44b69c:benchmarks/postgres_write/REPORT.md
```

To measure current main or explicit before/after binaries, use the
[repeatable benchmark guide](benchmarks/quotes_perf/README.md). It validates results
before timing and records artifact fingerprints. New measurements should remain
separate from these historical tables and retain their source/build provenance.
