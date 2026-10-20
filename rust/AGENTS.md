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
