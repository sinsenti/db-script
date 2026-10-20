#!/usr/bin/env python3
"""Live remote parity using only the repository's explicit disposable Compose DBs."""
from __future__ import annotations
import argparse,json,os,subprocess,sys
from pathlib import Path
from typing import Any
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1]

class RemoteRunner:
    def __init__(self,label:str,cmd:list[str],engine:str,readonly:bool,env:dict[str,str]):
        args=cmd+["--engine",engine]
        if label!="python":args=[*cmd,"--protocol","--engine",engine]
        if readonly:args.append("--read-only")
        self.proc=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",bufsize=1,env=env)
        self.label=label
    def request(self,obj:dict[str,Any])->dict[str,Any]:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(obj,ensure_ascii=False,separators=(",",":"))+"\n");self.proc.stdin.flush()
        line=self.proc.stdout.readline()
        if not line:
            err=self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"{self.label} remote runner exited without response: {err}")
        try:return json.loads(line)
        except json.JSONDecodeError as exc:raise RuntimeError(f"{self.label} emitted non-JSON stdout") from exc
    def close(self):
        if self.proc.stdin:self.proc.stdin.close()
        code=self.proc.wait(timeout=15);stderr=self.proc.stderr.read() if self.proc.stderr else ""
        if code:raise RuntimeError(f"{self.label} remote runner exited {code}: {stderr}")

def command_set(args):
    py=Path(args.python or (ROOT/".venv/bin/python" if (ROOT/".venv/bin/python").exists() else sys.executable))
    if not py.is_absolute(): py=ROOT/py
    py=py.absolute()
    commands={"python":[str(py),str(HERE/"remote_runner.py")]}
    paths={"go":Path(args.go_bin or ROOT/"go-sqlite/build/dbcli").resolve(),
           "rust":Path(args.rust_bin or ROOT.parent/"codex-rust-sqlite-benchmark/rust/target/release/dbcli-rust").resolve()}
    for name,path in paths.items():
        if path.is_file():commands[name]=[str(path)]
        elif args.require_all:raise FileNotFoundError(f"{name} binary not found: {path}")
        else:print(f"SKIP {name}: remote protocol binary not found",file=sys.stderr)
    return commands

def reset_fixture(py:list[str],engine:str,env:dict[str,str],action="reset"):
    result=subprocess.run(py+[str(HERE/"remote_fixture.py"),"--engine",engine,"--action",action],capture_output=True,text=True,env=env,timeout=60)
    if result.returncode:raise RuntimeError(f"{engine} disposable fixture {action} failed: {result.stderr.strip() or 'service or driver unavailable'}")
    return json.loads(result.stdout)

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--go-bin");parser.add_argument("--rust-bin");parser.add_argument("--python");parser.add_argument("--require-all",action="store_true");parser.add_argument("--output",type=Path,default=HERE/"build/remote-parity.json");args=parser.parse_args()
    commands=command_set(args);python_exec=[commands["python"][0]];env=os.environ.copy();spec=json.loads((HERE/"remote_workloads.json").read_text(encoding="utf-8"));failures=[];counts={};manifests={}
    for engine in ("postgres","mysql"):
        try:
            manifests[engine]=reset_fixture(python_exec,engine,env)
        except Exception as exc:
            report={"passed":False,"blocker":str(exc),"skipped":{"postgres":engine!="postgres","mysql":engine!="mysql"},"candidates":list(commands)}
            args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8");print(json.dumps(report,indent=2));return 2
        try:
            group=[x["request"] for x in spec["successful"]+spec["mutations"]]
            outputs={}
            for candidate,cmd in commands.items():
                reset_fixture(python_exec,engine,env)
                runner=RemoteRunner(candidate,cmd,engine,False,env)
                try:outputs[candidate]=[runner.request(req) for req in group]
                finally:runner.close()
            counts[f"{engine}_successful_and_mutation_requests"]=len(group)
            for candidate,values in outputs.items():
                if candidate=="python":continue
                for i,(expected,actual) in enumerate(zip(outputs["python"],values)):
                    if expected!=actual:failures.append({"engine":engine,"group":"success_mutations","candidate":candidate,"index":i,"request":group[i],"python":expected,"candidate_result":actual})
            for case in spec["rejections"]:
                responses={}
                for candidate,cmd in commands.items():
                    reset_fixture(python_exec,engine,env)
                    runner=RemoteRunner(candidate,cmd,engine,bool(case["read_only"]),env)
                    try:
                        response=runner.request(case["request"])
                        guard_table=LOOSE if case["request"].get("op") in ("raw","delete") else PEOPLE
                        guard=runner.request({"op":"count","table":guard_table})
                        responses[candidate]=(response,guard)
                    finally:runner.close()
                counts[f"{engine}_rejections"]=counts.get(f"{engine}_rejections",0)+1
                for candidate,value in responses.items():
                    if candidate!="python" and value!=responses["python"]:failures.append({"engine":engine,"group":"rejections","candidate":candidate,"name":case["name"],"request":case["request"],"python":responses["python"],"candidate_result":value})
        finally:
            try:reset_fixture(python_exec,engine,env,"drop")
            except Exception as exc:failures.append({"engine":engine,"cleanup_error":str(exc)})
    report={"protocol_version":1,"candidates":list(commands),"counts":counts,"fixture_manifests":manifests,"failures":failures,"passed":not failures}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8");print(json.dumps(report,indent=2,ensure_ascii=False));return 0 if not failures else 1

PEOPLE="dbcli_cmp_people";LOOSE="dbcli_cmp_loose"
if __name__=="__main__":raise SystemExit(main())
