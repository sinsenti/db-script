# Language comparison harness

The shared contract and workload source is [`PROTOCOL.md`](PROTOCOL.md). Do not create per-language fixture or timing harness variants. `fixture.py`, `workloads.json`, `python_runner.py`, `parity.py`, and `benchmark.py` must be copied byte-for-byte between the Go and Rust worktrees.

## SQLite correctness gate

From the repository root, build optimized candidate executables and run the shared suite:

```sh
go-sqlite/build.sh
cargo build --release --manifest-path ../codex-rust-sqlite-benchmark/rust/Cargo.toml
python3 benchmarks/language_comparison/parity.py --require-all \
  --go-bin go-sqlite/build/dbcli \
  --rust-bin ../codex-rust-sqlite-benchmark/rust/target/release/dbcli-rust
```

Parity compares Python production `SQLiteAdapter` and production UI table renderer against both native candidates, including SQL text, bound parameters, ordered typed results (NULL and base64-tagged BLOBs), schema, foreign keys, rendering, ordered mutation workflows and rejections. It validates both the small correctness fixture and every large-fixture benchmark request before setting `passed: true`. Generated fixtures and default machine reports live in ignored `benchmarks/language_comparison/build/`.

## Timing gate

Do not time until the three-way parity gate passes and both release binaries are ready. The runner refuses to proceed without that result and takes a nonblocking exclusive `flock` on the shared host lock `/tmp/db-script-language-comparison.benchmark.lock` (not inside one worktree). Example:

```sh
python3 benchmarks/language_comparison/benchmark.py --samples 30 --warmups 3 \
  --go-bin go-sqlite/build/dbcli \
  --rust-bin ../codex-rust-sqlite-benchmark/rust/target/release/dbcli-rust \
  --output benchmarks/language_comparison/build/results.json
```

The runner randomizes candidate and operation order, includes fresh-process startup and fresh-process first-touch database samples, and measures repeated warm-process operations. It reports wall time, process CPU tick deltas, Linux `VmHWM` in bytes, response/trace sizes and raw samples. Warm CRUD uses a newly copied identical fixture for each operation; fixture-copy time is excluded. Fresh-process/first-touch is **not** an OS-cache-cold run because no cache eviction is performed. All protocol timings include JSONL serialization, IPC and SQL trace instrumentation and must not be interpreted as bare database latency.

The report must include toolchain/SQLite/OS details, fixture hash/seed/size, artifact/runtime distribution context, reproducible commands, correctness results and concrete source/dependency/build observations. Do not report skipped live services as passes.

## Remote adapter comparison

The selected PostgreSQL/MySQL protocol, approved disposable Compose endpoints, native parity contract and runner commands are documented in [`REMOTE.md`](REMOTE.md). `remote_parity.py --require-all` gates both engines against production Python and both native candidates; it recreates and drops only the fixed `dbcli_cmp_*` fixtures. The remote benchmark additionally requires passing three-way SQLite parity and the same host-wide exclusive timing lock. Remote timings are separate from SQLite timings because network/server round trips dominate.
