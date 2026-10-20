#!/usr/bin/env python3
"""Interleaved shared SQLite benchmark. Timings include JSONL/trace IPC overhead."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from fixture import create_fixture
from parity import Runner

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
HZ = os.sysconf("SC_CLK_TCK")


def read_proc(pid: int) -> tuple[float | None, float | None, int | None]:
    """Return this process's user/system CPU seconds and peak RSS bytes (Linux)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2 :].split()  # begins with field 3
        user = int(fields[11]) / HZ  # field 14
        system = int(fields[12]) / HZ  # field 15
        status = Path(f"/proc/{pid}/status").read_text()
        rss_kib = next(int(line.split()[1]) for line in status.splitlines()
                       if line.startswith("VmHWM:"))
        return user, system, rss_kib * 1024
    except (OSError, ValueError, StopIteration, IndexError):
        return None, None, None


def measure_request(runner: Runner, request: dict[str, Any], startup: bool = False) -> dict[str, Any]:
    pid = runner.proc.pid
    before = read_proc(pid)
    start = time.perf_counter_ns()
    response = runner.request(request)
    elapsed = time.perf_counter_ns() - start
    after = read_proc(pid)
    if not response.get("ok"):
        raise RuntimeError(f"benchmark request failed for {runner.label}: {response}")
    cpu_user = None if before[0] is None or after[0] is None else max(0.0, after[0] - before[0])
    cpu_system = None if before[1] is None or after[1] is None else max(0.0, after[1] - before[1])
    body = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"candidate": runner.label, "wall_ns": elapsed, "user_cpu_s": cpu_user,
            "system_cpu_s": cpu_system, "peak_rss_bytes": after[2],
            "response_bytes": len(body), "trace_calls": len(response.get("trace", [])),
            "result_ok": True, "startup_included": startup}


def command_set(args) -> dict[str, list[str]]:
    commands = {"python": [sys.executable, str(HERE / "python_runner.py")]}
    paths = {
        "go": Path(args.go_bin or ROOT / "go-sqlite/build/dbcli").resolve(),
        "rust": Path(args.rust_bin or ROOT.parent / "codex-rust-sqlite-benchmark/rust/target/release/dbcli-rust").resolve(),
    }
    for name, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"{name} release binary not found: {path}")
        commands[name] = [str(path)]
    return commands


def proc_args(candidate: str, command: list[str], db: Path) -> list[str]:
    if candidate == "python":
        return command + ["--db", str(db)]
    return command + ["--protocol", "--db", str(db)]


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    def percentile(values: list[float], q: float) -> float | None:
        if not values:
            return None
        values = sorted(values)
        index = int(round((len(values) - 1) * q))
        return values[index]
    walls = [s["wall_ns"] / 1e6 for s in samples]
    rss = [s["peak_rss_bytes"] for s in samples if s["peak_rss_bytes"] is not None]
    users = [s["user_cpu_s"] for s in samples if s["user_cpu_s"] is not None]
    systems = [s["system_cpu_s"] for s in samples if s["system_cpu_s"] is not None]
    return {"n":len(samples), "wall_ms":{"min":min(walls),"median":percentile(walls,.5),"p05":percentile(walls,.05),"p95":percentile(walls,.95),"max":max(walls)},
            "user_cpu_s_median":percentile(users,.5), "system_cpu_s_median":percentile(systems,.5),
            "peak_rss_bytes_max":max(rss) if rss else None}


def candidate_metadata(commands: dict[str, list[str]]) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for name, command in commands.items():
        executable = Path(command[0]).resolve()
        info[name] = {"command":command, "executable":str(executable),
                      "artifact_bytes":executable.stat().st_size if name!="python" else None}
    info["python"].update({"runtime":sys.version, "executable_bytes":Path(sys.executable).resolve().stat().st_size,
                           "dependencies":{"prompt_toolkit":dependency_version("prompt_toolkit")}})
    info["go"].update({"toolchain":command_version(["go","version"])})
    info["rust"].update({"toolchain":command_version(["rustc","--version"]),"cargo":command_version(["cargo","--version"])})
    return info


def dependency_version(module: str) -> str | None:
    try:
        from importlib.metadata import version
        return version("prompt-toolkit") if module=="prompt_toolkit" else None
    except Exception:
        return None


def command_version(command: list[str]) -> str | None:
    try:
        return subprocess.run(command,capture_output=True,text=True,check=True,timeout=5).stdout.strip()
    except Exception:
        return None


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--go-bin")
    parser.add_argument("--rust-bin")
    parser.add_argument("--samples",type=int,default=30)
    parser.add_argument("--warmups",type=int,default=3)
    parser.add_argument("--seed",type=int,default=20250308)
    parser.add_argument("--parity-file",type=Path,default=HERE/"build/parity.json")
    parser.add_argument("--output",type=Path,default=HERE/"results-local.json")
    parser.add_argument("--lock",type=Path,default=Path("/tmp/db-script-language-comparison.benchmark.lock"))
    args=parser.parse_args()
    if args.samples<5: parser.error("use at least 5 samples")
    if not args.parity_file.is_file(): parser.error("shared parity output is missing; run parity.py --require-all first")
    parity=json.loads(args.parity_file.read_text(encoding="utf-8"))
    if not parity.get("passed") or not {"python","go","rust"}.issubset(parity.get("candidates",[])):
        parser.error("benchmark blocked: three-candidate parity did not pass")
    commands=command_set(args)
    args.lock.parent.mkdir(parents=True,exist_ok=True)
    lock_file=args.lock.open("w")
    try:
        fcntl.flock(lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error(f"exclusive timing lock is held: {args.lock}")
    rng=random.Random(args.seed)
    spec=json.loads((HERE/"workloads.json").read_text(encoding="utf-8"))
    benchmark={item["name"]:item["request"] for item in spec["benchmark"]}
    fixture_path=HERE/"build/fixture_large.sqlite"
    fixture_path.parent.mkdir(parents=True,exist_ok=True)
    manifest=create_fixture(fixture_path,"large")
    expected_names=sorted(set(benchmark)-{"startup_first_query"})
    raw=[];versions={}
    with tempfile.TemporaryDirectory(prefix="dbcli-benchmark-") as tdir:
        temp=Path(tdir)
        candidates=list(commands)
        for round_index in range(args.samples):
            # Randomized startup ordering, then fresh-process first-touch runs.
            order=candidates[:];rng.shuffle(order)
            live={}
            for candidate in order:
                db=temp/f"warm-{candidate}.sqlite";shutil.copy2(fixture_path,db)
                launch_start=time.perf_counter_ns()
                runner=Runner(candidate,commands[candidate],db,False)
                spawn_done=time.perf_counter_ns()
                live[candidate]=runner
                sample=measure_request(runner,benchmark["startup_first_query"],startup=True)
                request_wall=sample["wall_ns"]
                sample["wall_ns"]=time.perf_counter_ns()-launch_start
                sample["spawn_ns"]=spawn_done-launch_start
                sample["request_after_spawn_ns"]=request_wall
                raw.append({"round":round_index,"phase":"fresh_process_startup_first_query","operation":"startup_first_query",**sample})
                version=runner.request({"op":"raw","sql":"SELECT sqlite_version() AS version","limit":1,"offset":0})
                versions[candidate]=version.get("result",{}).get("rows",[[None]])[0][0]
            first_order=candidates[:];rng.shuffle(first_order)
            for candidate in first_order:
                db=temp/f"touch-{candidate}.sqlite";shutil.copy2(fixture_path,db)
                launch_start=time.perf_counter_ns()
                runner=Runner(candidate,commands[candidate],db,False)
                spawn_done=time.perf_counter_ns()
                sample=measure_request(runner,benchmark["page_sorted"],startup=True)
                request_wall=sample["wall_ns"]
                sample["wall_ns"]=time.perf_counter_ns()-launch_start
                sample["spawn_ns"]=spawn_done-launch_start
                sample["request_after_spawn_ns"]=request_wall
                raw.append({"round":round_index,"phase":"fresh_process_first_touch_database_not_os_cold","operation":"page_sorted",**sample})
                runner.close()
            # Prime each long-lived process and then randomly interleave every warm request.
            for candidate,runner in live.items():
                for _ in range(args.warmups):
                    runner.request(benchmark["page_sorted"])
            schedule=[(candidate,name) for candidate in candidates for name in expected_names]
            rng.shuffle(schedule)
            for candidate,name in schedule:
                request=benchmark[name]
                if name in {"insert","update","delete"}:
                    shutil.copy2(fixture_path,temp/f"warm-{candidate}.sqlite")
                sample=measure_request(live[candidate],request)
                raw.append({"round":round_index,"phase":"warm_process_operation_jsonl_ipc_and_trace_included","operation":name,**sample})
            for runner in live.values():runner.close()
    grouped={}
    for sample in raw:
        key=(sample["phase"],sample["operation"],sample["candidate"])
        grouped.setdefault(key,[]).append(sample)
    summaries={"/".join(key):summarize(values) for key,values in grouped.items()}
    output={"schema_version":1,"seed":args.seed,"samples":args.samples,"warmups":args.warmups,
            "parity_file":str(args.parity_file),"fixture":manifest,"candidates":candidate_metadata(commands),
            "environment":{"platform":platform.platform(),"machine":platform.machine(),"processor":platform.processor(),
                           "cpu_count":os.cpu_count(),"python":sys.version,"go":command_version(["go","version"]),
                           "rustc":command_version(["rustc","--version"]),"sqlite_versions":versions,
                           "sqlite_fixture_settings":"journal_mode=DELETE; adapter enables foreign_keys=ON on each connection"},
            "measurement_notes":{"cold":"fresh process / fresh database copy; the OS page cache was not evicted, so these are not OS-cache-cold runs",
                                 "warm":"persistent candidate process, repeated operations; adapters still use short-lived DB connections",
                                 "instrumentation":"all wall times include JSONL encoding, pipe IPC, response decoding, and SQL trace collection; RSS is Linux /proc VmHWM in bytes; per-op CPU is process tick delta",
                                 "database_reset":"each mutating operation copies the identical large fixture before timing; copy time is excluded"},
            "summary":summaries,"raw_samples":raw}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(output,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    print(f"wrote {args.output} ({len(raw)} samples); parity gate passed; lock={args.lock}")
    return 0

if __name__=="__main__":raise SystemExit(main())
