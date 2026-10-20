#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ "${1:-}" = test ]; then cargo test --locked; fi
exec cargo build --release --locked
