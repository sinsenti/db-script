# Restoring archived Rust comparisons

The Rust worktree cleanup on 2026-10-22 (Europe/Warsaw) removed 20 worktrees,
including the mixed `pi_go_rust_compare` worktree. Go-only, Python and the other
integration/comparison worktrees remain. The filesystem free-space increase was
approximately **31.82 GiB**, after creating an approximately **74.02 MiB** archive.
These are local observations; filesystem activity can affect the free-space delta.

## Review outcome

The selected Original Rust backend is already in main. The old Ratatui snapshot
is the source of main's UI and predates its later export protection, compact
Neovim plans and worker deadlines/cancellation. Comparing the latest alternative
Rust sources found:

- Contextual completion is byte-identical to main in both direct Codex and separate
  Rust snapshots.
- PostgreSQL write handling is byte-identical to main in those snapshots.
- Main's PostgreSQL read helper additionally contains the generated-column and
  ALWAYS-identity SQL export changes.
- The alternatives retain distinct SQLite adapters, protocol implementations,
  renderers and independent safety-test corpora. These remain useful for future
  reference and are preserved by their branches and the archive.

No additional production feature was ported during cleanup. This was a source
review and preservation operation; application tests and benchmarks were not run,
and no database was opened. Main's runtime paths resolve inside main and do not
require the removed sibling worktrees. Existing app.rs/view.rs working changes
were left untouched.

## What was preserved

All original branch names still point to their previous commits. The original
uncommitted Rust prototype also has this recovery branch:

`archive/codex_rust_sqlite_benchmark_20261021T221442Z`

Snapshot commit: `09de9cb9f5ce3881f96dd3928ec89c223ba3c050`.

A private archive is stored outside the removed directories:

`/home/user/git/personal/db-script__archives/rust-worktrees-20261021T221442Z`

It contains:

- Per-worktree tarballs with sources, non-cache local artifacts and optimized
  release executables. Local symlinks are preserved without following them.
- `manifest.json` with original paths, branch/commit pins, every archived file's
  SHA-256, executable paths and excluded rebuildable cache directories.
- `rust-history.bundle`, retaining the selected Git history independently of the
  working repository.
- `SHA256SUMS` and `removal-results.json`.

Every archived regular file/symlink was checked against its tar member before
removal, and original files were checked again immediately before deletion.
The Git bundle was verified. Cargo targets/dependency build caches, virtualenvs
and Python bytecode were excluded, except for preserved release executables.
The archive is private (directory 0700, files 0600): local fixture metadata may
contain credentials, so its contents should not be published.

## Restore source from a retained branch

Run from the main repository. For example:

```bash
git worktree add ../db-script__worktrees/codex_codex_rust_quotes codex_codex_rust_quotes
```

To recover the uncommitted prototype snapshot at a new location:

```bash
git worktree add ../db-script__worktrees/restored_rust_prototype archive/codex_rust_sqlite_benchmark_20261021T221442Z
```

Rebuild using that revision's own Cargo manifest and instructions, or restore the
corresponding archived release executable. Branch recovery alone does not restore
ignored binaries. Historical multi-variant launchers that point to removed sibling
paths require those worktrees/binaries to be restored; main's normal launcher is
independent of them.

The independent Codex safety cases remain inspectable without any worktree:

```bash
git show codex_codex_rust_quotes:rust/tests/safety.rs
```

## Removed worktrees and retained revisions

| Branch | Original HEAD |
| --- | --- |
| `codex_rust_sqlite_benchmark` | `57c98926cd51` |
| `codex_codex_rust_pg_write` | `dc021a379abf` |
| `codex_codex_rust_postgres` | `3b8c4e8f14e9` |
| `codex_codex_rust_quotes` | `04b82bd5fe3c` |
| `codex_codex_rust_sqlite_benchmark` | `8f1e3e583c10` |
| `codex_codex_rust_writable` | `451dcaad4454` |
| `codex_rust_main_merge` | `54f82a2d3952` |
| `codex_rust_pg_write_original` | `2e21d2515421` |
| `codex_rust_pg_write_pi` | `5b40abad5ec6` |
| `codex_rust_pg_write_ui` | `646f173702b9` |
| `codex_rust_postgres_original` | `5567f42feccd` |
| `codex_rust_postgres_pi` | `4f5a2065356e` |
| `codex_rust_quotes_neovim` | `24fc85ebd8b8` |
| `codex_rust_quotes_original` | `c51eae7fa2b3` |
| `codex_rust_quotes_pi` | `c3040c7dcd76` |
| `codex_rust_ui_v2` | `5e642965776b` |
| `codex_rust_writable_original` | `92dd60d73be2` |
| `codex_rust_writable_pi` | `0f005202aad5` |
| `pi_go_rust_compare` | `5f6d11ee26f4` |
| `pi_rust_rewrite` | `e8d57d18db18` |
