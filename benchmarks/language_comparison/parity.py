#!/usr/bin/env python3
"""Run the shared correctness/equivalence protocol against Python and native binaries."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from fixture import create_fixture

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


class Runner:
    def __init__(self, label: str, cmd: list[str], db: Path, readonly: bool):
        args = cmd + (["--protocol", "--db", str(db)] if label != "python" else ["--db", str(db)])
        if readonly:
            args.append("--read-only")
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self.label = label

    def request(self, obj: dict[str, Any]) -> dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"{self.label} runner exited without a response: {err}")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.label} emitted non-JSON stdout: {line!r}") from exc

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        code = self.proc.wait(timeout=10)
        stderr = self.proc.stderr.read() if self.proc.stderr else ""
        if code:
            raise RuntimeError(f"{self.label} runner exited {code}: {stderr}")


def command_set(args) -> dict[str, list[str]]:
    result = {"python": [sys.executable, str(HERE / "python_runner.py")]}
    go = Path(args.go_bin).resolve() if args.go_bin else ROOT / "go-sqlite/build/dbcli"
    rust = Path(args.rust_bin).resolve() if args.rust_bin else ROOT.parent / "codex-rust-sqlite-benchmark/rust/target/release/dbcli-rust"
    for name, path in (("go", go), ("rust", rust)):
        if path.exists():
            result[name] = [str(path)]
        elif args.require_all:
            raise FileNotFoundError(f"{name} binary not found: {path}")
        else:
            print(f"SKIP {name}: binary not found at {path}", file=sys.stderr)
    return result


def run_group(commands: dict[str, list[str]], fixture: Path, requests: list[dict],
              readonly: bool, tmp: Path, label: str) -> tuple[int, list[dict]]:
    outputs: dict[str, list[dict]] = {}
    for candidate, cmd in commands.items():
        db = tmp / f"{candidate}-{label}.sqlite"
        shutil.copy2(fixture, db)
        runner = Runner(candidate, cmd, db, readonly)
        try:
            outputs[candidate] = [runner.request(req) for req in requests]
        finally:
            runner.close()
    oracle = outputs["python"]
    failures = []
    for candidate, values in outputs.items():
        if candidate == "python":
            continue
        for i, (expected, actual) in enumerate(zip(oracle, values)):
            if actual != expected:
                failures.append({"candidate": candidate, "index": i,
                                 "request": requests[i], "python": expected, "candidate_result": actual})
        if len(values) != len(oracle):
            failures.append({"candidate": candidate, "error": "response count mismatch"})
    return sum(len(requests) for _ in commands), failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--go-bin")
    parser.add_argument("--rust-bin")
    parser.add_argument("--require-all", action="store_true")
    parser.add_argument("--workloads", type=Path, default=HERE / "workloads.json")
    parser.add_argument("--output", type=Path, default=HERE / "build/parity.json")
    args = parser.parse_args()
    commands = command_set(args)
    spec = json.loads(args.workloads.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    failures: list[dict] = []
    totals: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="dbcli-parity-") as temp:
        tmp = Path(temp)
        fixture = tmp / "fixture_small.sqlite"
        create_fixture(fixture, "small")
        successful = [x["request"] for x in spec["successful"]]
        count, bad = run_group(commands, fixture, successful, False, tmp, "success")
        totals["successful_requests"] = len(successful)
        failures.extend({"group": "successful", **x} for x in bad)
        benchmark_ops = spec["benchmark"]
        benchmark_reads = [x for x in benchmark_ops if x["name"] not in {"insert", "update", "delete"}]
        large_fixture = tmp / "fixture_large.sqlite"
        create_fixture(large_fixture, "large")
        count, bad = run_group(commands, large_fixture, [x["request"] for x in benchmark_reads], False, tmp, "benchmark-reads")
        totals["large_benchmark_read_requests"] = len(benchmark_reads)
        failures.extend({"group": "benchmark_reads", "name": benchmark_reads[x.get("index", 0)]["name"] if "index" in x else None, **x} for x in bad)
        for item in benchmark_ops:
            if item["name"] not in {"insert", "update", "delete"}:
                continue
            responses = {}
            for candidate, cmd in commands.items():
                db = tmp / f"{candidate}-benchmark-{item['name']}.sqlite"
                shutil.copy2(large_fixture, db)
                runner = Runner(candidate, cmd, db, False)
                try:
                    responses[candidate] = runner.request(item["request"])
                finally:
                    runner.close()
            totals["large_benchmark_mutation_requests"] = totals.get("large_benchmark_mutation_requests", 0) + 1
            for candidate in commands:
                if candidate != "python" and responses[candidate] != responses["python"]:
                    failures.append({"group":"benchmark_mutations", "candidate":candidate,
                                     "name":item["name"], "request":item["request"],
                                     "python":responses["python"], "candidate_result":responses[candidate]})
        mutations = [x["request"] for x in spec["mutations"]]
        # A mutation sequence shares one fresh copy per candidate and preserves order.
        outputs = {}
        for candidate, cmd in commands.items():
            db = tmp / f"{candidate}-mutations.sqlite"
            shutil.copy2(fixture, db)
            runner = Runner(candidate, cmd, db, False)
            try:
                outputs[candidate] = [runner.request(req) for req in mutations]
            finally:
                runner.close()
        totals["mutation_requests"] = len(mutations)
        for candidate, values in outputs.items():
            if candidate != "python":
                for i, (expected, actual) in enumerate(zip(outputs["python"], values)):
                    if actual != expected:
                        failures.append({"group":"mutations","candidate":candidate,"index":i,
                                         "name":spec["mutations"][i]["name"],
                                         "request":mutations[i],"python":expected,"candidate_result":actual})
        totals["rejection_requests"] = len(spec["rejections"])
        for case in spec["rejections"]:
            req = case["request"]
            responses = {}
            for candidate, cmd in commands.items():
                db = tmp / f"{candidate}-reject-{case['name']}.sqlite"
                shutil.copy2(fixture, db)
                runner = Runner(candidate, cmd, db, bool(case["read_only"]))
                try:
                    responses[candidate] = runner.request(req)
                    guard_table = "loose_tags" if req.get("op") in ("raw", "delete") else "people"
                    guard = runner.request({"op":"count","table":guard_table})
                    responses[candidate + "_guard"] = guard
                finally:
                    runner.close()
            for candidate in commands:
                if candidate == "python":
                    continue
                if responses[candidate] != responses["python"]:
                    failures.append({"group":"rejections","candidate":candidate,
                                     "name":case["name"],"request":req,
                                     "python":responses["python"],"candidate_result":responses[candidate]})
                if responses[candidate + "_guard"] != responses["python_guard"]:
                    failures.append({"group":"rejection_state_guard","candidate":candidate,
                                     "name":case["name"],"python":responses["python_guard"],
                                     "candidate_result":responses[candidate + "_guard"]})
    report = {"protocol_version": 1, "candidates": list(commands), "counts": totals,
              "failures": failures, "passed": not failures}
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
