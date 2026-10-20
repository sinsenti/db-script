#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
cargo build --release --locked --manifest-path rust/Cargo.toml
cargo build --release --locked --manifest-path ui/rust/Cargo.toml
