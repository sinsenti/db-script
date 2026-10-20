#!/usr/bin/env python3
"""Interleaved remote DB-API benchmark; requires full remote and SQLite parity."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import random
import sys
import time
from pathlib import Path
from typing import Any
from importlib.metadata import PackageNotFoundError, version

from benchmark import command_version, read_proc, summarize
from remote_fixture import configured_url
from remote_parity import RemoteRunner, command_set, reset_fixture

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HZ = os.sysconf("SC_CLK_TCK")


def distribution_version(distribution: str) -> str | None:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


REQUIRED = {"python", "go", "rust"}
MUTATIONS = {"insert", "update", "delete"}


def measure(runner: RemoteRunner, request: dict[str, Any], engine: str) -> dict[str, Any]:
    before = read_proc(runner.proc.pid)
    start = time.perf_counter_ns()
    response = runner.request(request)
    elapsed = time.perf_counter_ns() - start
    after = read_proc(runner.proc.pid)
    if not response.get("ok"):
        raise RuntimeError(f"benchmark request failed for {runner.label}/{engine}: {response}")
    body = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {
        "candidate": runner.label,
        "engine": engine,
        "wall_ns": elapsed,
        "user_cpu_s": None if before[0] is None or after[0] is None else max(0.0, after[0] - before[0]),
        "system_cpu_s": None if before[1] is None or after[1] is None else max(0.0, after[1] - before[1]),
        "peak_rss_bytes": after[2],
        "response_bytes": len(body),
        "trace_calls": len(response.get("trace", [])),
        "result_ok": True,
    }


def checked_parity(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"{label} parity output is missing: {path}")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not result.get("passed") or not REQUIRED.issubset(result.get("candidates", [])):
        raise ValueError(f"{label} parity must pass for Python, Go and Rust")
    return result


def candidate_metadata(commands: dict[str, list[str]]) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for name, command in commands.items():
        executable = Path(command[0]).resolve()
        info[name] = {
            "command": command,
            "executable": str(executable),
            "artifact_bytes": executable.stat().st_size if name != "python" else None,
        }
    info["python"].update({
        "runtime": sys.version,
        "executable_bytes": Path(sys.executable).resolve().stat().st_size,
        "dependencies": {
            "psycopg": distribution_version("psycopg"),
            "mysql-connector-python": distribution_version("mysql-connector-python"),
        },
    })
    info["go"].update({"toolchain": command_version(["go", "version"])})
    info["rust"].update({
        "toolchain": command_version(["rustc", "--version"]),
        "cargo": command_version(["cargo", "--version"]),
    })
    return info


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--go-bin")
    parser.add_argument("--rust-bin")
    parser.add_argument("--python")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20250308)
    parser.add_argument("--remote-parity-file", type=Path, default=HERE / "build/remote-parity.json")
    parser.add_argument("--sqlite-parity-file", type=Path, default=HERE / "build/parity.json")
    parser.add_argument("--output", type=Path, default=HERE / "build/remote-results.json")
    parser.add_argument("--lock", type=Path, default=Path("/tmp/db-script-language-comparison.benchmark.lock"))
    args = parser.parse_args()
    if args.samples < 5:
        parser.error("use at least 5 samples")

    try:
        remote_parity = checked_parity(args.remote_parity_file, "remote")
        sqlite_parity = checked_parity(args.sqlite_parity_file, "SQLite")
        # Validate both endpoints before any fixture reset or candidate launch.
        configured_url("postgres")
        configured_url("mysql")
        args.require_all = True
        commands = command_set(args)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        parser.error(str(exc))

    args.lock.parent.mkdir(parents=True, exist_ok=True)
    lock_file = args.lock.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error(f"exclusive timing lock is held: {args.lock}")

    spec = json.loads((HERE / "remote_workloads.json").read_text(encoding="utf-8"))
    benchmark = {item["name"]: item["request"] for item in spec["benchmark"]}
    candidates = list(commands)
    rng = random.Random(args.seed)
    env = os.environ.copy()
    raw: list[dict[str, Any]] = []
    manifests: dict[str, Any] = {}

    for engine in ("postgres", "mysql"):
        live: dict[str, RemoteRunner] = {}
        try:
            order = candidates[:]
            rng.shuffle(order)
            for candidate in order:
                manifest = reset_fixture([commands["python"][0]], engine, env)
                manifests.setdefault(engine, manifest)
                launch_start = time.perf_counter_ns()
                runner = RemoteRunner(candidate, commands[candidate], engine, False, env)
                live[candidate] = runner
                response_sample = measure(runner, benchmark["startup_connection"], engine)
                request_wall = response_sample["wall_ns"]
                response_sample["wall_ns"] = time.perf_counter_ns() - launch_start
                response_sample["spawn_ns"] = max(0, response_sample["wall_ns"] - request_wall)
                response_sample["request_after_spawn_ns"] = request_wall
                response_sample["startup_included"] = True
                raw.append({
                    "round": -1,
                    "phase": "fresh_process_startup_and_remote_connection",
                    "operation": "startup_connection",
                    **response_sample,
                })

            for runner in live.values():
                for _ in range(args.warmups):
                    runner.request(benchmark["page_sorted"])

            schedule = [
                (candidate, name)
                for candidate in candidates
                for name in benchmark
                if name != "startup_connection"
            ]
            for round_index in range(args.samples):
                round_schedule = schedule[:]
                rng.shuffle(round_schedule)
                for candidate, name in round_schedule:
                    if name in MUTATIONS:
                        # The deterministic reset is outside the timed request.
                        reset_fixture([commands["python"][0]], engine, env)
                    sample = measure(live[candidate], benchmark[name], engine)
                    raw.append({
                        "round": round_index,
                        "phase": "warm_process_remote_connection_and_jsonl_ipc",
                        "operation": name,
                        "fixture_reset_before_request": name in MUTATIONS,
                        **sample,
                    })
        finally:
            close_errors = []
            for runner in live.values():
                try:
                    runner.close()
                except Exception as exc:
                    close_errors.append(exc)
            try:
                reset_fixture([commands["python"][0]], engine, env, "drop")
            except Exception:
                # Report the cleanup failure without echoing environment values.
                raise
            if close_errors:
                raise close_errors[0]

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for sample in raw:
        key = (sample["engine"], sample["phase"], sample["operation"], sample["candidate"])
        grouped.setdefault(key, []).append(sample)
    summaries = {"/".join(key): summarize(values) for key, values in grouped.items()}
    output = {
        "schema_version": 1,
        "seed": args.seed,
        "samples": args.samples,
        "warmups": args.warmups,
        "remote_parity_file": str(args.remote_parity_file),
        "sqlite_parity_file": str(args.sqlite_parity_file),
        "remote_parity": remote_parity,
        "sqlite_parity": sqlite_parity,
        "fixtures": manifests,
        "candidates": candidate_metadata(commands),
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "python": sys.version,
            "go": command_version(["go", "version"]),
            "rustc": command_version(["rustc", "--version"]),
        },
        "measurement_notes": {
            "remote": "Network and database-server latency dominate these native adapter requests; do not compare directly with local SQLite timings.",
            "connection": "Each adapter operation opens and closes a database connection. Startup includes process spawn and the first network connection/request.",
            "instrumentation": "Wall times include JSONL encoding, IPC, response decoding and SQL trace collection. Process CPU and Linux /proc VmHWM are reported when available.",
            "mutation_reset": "The disposable fixture is reset before each timed mutation; fixture reset time is excluded. This reset runs against only the fixed dbcli_cmp_* objects on the approved Compose endpoints.",
        },
        "summary": summaries,
        "raw_samples": raw,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(raw)} samples); three-candidate parity passed; lock={args.lock}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
