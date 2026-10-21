# Selected native Rust backend

This is the Original Rust backend selected for main. Keep native runtime independent
of Python and benchmark harnesses. Main's Ratatui launcher is tools/rust.py; it always
selects the original-rust protocol and a worker in this same checkout.

SQLite is bundled with FTS5. PostgreSQL uses environment credentials, short-lived
bounded native connections and read-only transactions for raw reads. Writes require
complete original PKs, typed binding, stale-snapshot checks and atomic affected-row
validation. Preserve JSON-null/SQL-NULL, exact numeric/UUID/bytea/array semantics.

E opens a private Neovim diff, then count-only confirmation; omissions never delete.
Preserve terminal restoration, child reaping, 0700/0600 temporary storage and cleanup.
Run cargo test --locked --manifest-path rust/Cargo.toml and the Python offline suite.
Real PTY/mutation checks must use disposable fixtures; never use the application DB.
See ../RUST.md and POSTGRES_WRITABLE.md for authoritative commands and limitations.

PostgreSQL SQL exports omit generated columns, override ALWAYS identity values,
and use DEFAULT VALUES when the projected insertable column list is empty.
Keep these semantics aligned with ui/rust/src/data.rs. New live replay tests in
both Rust crates validate a marked disposable fixture via tests/support only.
Standalone performance tooling lives in ../benchmarks/quotes_perf and resolves
main-local paths; it must remain outside the production runtime dependency graph.

Rust export destinations use export_file::write_new (create_new, mode 0600,
cleanup on error); never reintroduce fs::write/from_path overwrites in UI exports.
E editor originals/deletes are compact PK tokens; retain complete immutable
snapshots in memory and expand only requested actions before strict validators.
Keep the duplicated export_file.rs and bulk_editor.rs modules aligned across
rust/ and ui/rust/. Protocol apply_batch originals remain full snapshots.
