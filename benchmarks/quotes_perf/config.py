"""Local paths and explicit optional candidate manifests; no worktree discovery."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from common import ROOT, table_parts

WORKER = ROOT / 'rust/target/release/dbcli-rust'
FRONTEND = ROOT / 'ui/rust/target/release/dbcli-ui-rust'
PROTOCOL_ALIASES = {'dispatch':'original-rust','batch':'codex-rust','rewrite':'pi-rust'}


def parser(description, *, ui=False):
    p = argparse.ArgumentParser(description=description)
    source = p.add_mutually_exclusive_group()
    source.add_argument('--container', default='a1labs-postgres-1')
    source.add_argument('--from-env', action='store_true', help='read DBCLI_POSTGRES_URL; never pass credentials in arguments')
    p.add_argument('--table', default='public.quotes', help='schema-qualified table; primary key required')
    p.add_argument('--repetitions', type=int, default=3 if ui else 9)
    p.add_argument('--seed', type=int, default=20261020)
    p.add_argument('--worker', type=Path, default=WORKER)
    p.add_argument('--manifest', type=Path, help='optional explicit native candidate JSON list')
    p.add_argument('--candidates', nargs='+', help='names from manifest, original-rust or python-reference')
    p.add_argument('--output', type=Path, default=ROOT/'.local/performance'/('ui-results.json' if ui else 'backend-results.json'))
    p.add_argument('--overwrite', action='store_true', help='replace an existing report')
    if ui:
        p.add_argument('--frontend', type=Path, default=FRONTEND)
        p.add_argument('--interfaces', nargs='+', choices=['modern','legacy'], default=['modern'])
        p.add_argument('--nvim', default='nvim', help='Neovim executable, resolved through PATH')
    else:
        p.add_argument('--filter', default=None, help='trusted WHERE predicate; defaults to first PK IS NOT NULL')
    return p


def candidates(args, *, ui=False):
    table_parts(args.table)
    if args.repetitions < 1: raise ValueError('Repetitions must be positive')
    if args.output.exists() and not args.overwrite: raise ValueError('Output already exists; choose a new --output or pass --overwrite')
    records = [{'name':'original-rust','protocol':'dispatch','worker':str(args.worker)}]
    if args.manifest:
        records = json.loads(args.manifest.read_text())
        if not isinstance(records,list) or not records: raise ValueError('Manifest must be a nonempty candidate list')
    result = {}
    for record in records:
        name = record['name']
        if not isinstance(name,str) or not name or name in result or name == 'python-reference':
            raise ValueError('Candidate names must be unique; python-reference is reserved')
        if record['protocol'] not in PROTOCOL_ALIASES: raise ValueError('Unknown candidate protocol')
        worker = Path(record['worker']).expanduser()
        if not worker.is_absolute(): worker = (args.manifest.parent if args.manifest else ROOT) / worker
        result[name] = dict(record,worker=str(worker.resolve()))
    if not ui: result['python-reference'] = {'name':'python-reference','protocol':'python-reference','worker':sys.executable}
    selected = args.candidates or list(result)
    if len(set(selected)) != len(selected) or any(name not in result for name in selected):
        raise ValueError('Choose unique candidates present in the manifest')
    result = {name:result[name] for name in selected}
    for record in result.values():
        if not os.access(record['worker'],os.X_OK): raise ValueError('Candidate executable unavailable; build first')
    return result


def artifact(path):
    path = Path(path).resolve()
    hasher = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1048576),b''): hasher.update(chunk)
    digest = hasher.hexdigest()
    return {'path':str(path),'sha256':digest,'bytes':path.stat().st_size}


def provenance(records, frontend=None):
    executables = {Path(record['worker']).resolve() for record in records.values()}
    if frontend: executables.add(Path(frontend).resolve())
    paths = set(executables)
    paths.update(Path(__file__).parent.glob('*.py'))
    paths.add(Path(__file__).with_name('requirements.txt'))
    paths.update((ROOT/'tools').glob('postgres_target.py'))
    for folder, pattern in [('rust/src','*.rs'),('ui/rust/src','*.rs'),('dbcli','*.py')]:
        paths.update((ROOT/folder).rglob(pattern))
    paths.update(ROOT/path for path in ['rust/Cargo.lock','rust/Cargo.toml','ui/rust/Cargo.lock','ui/rust/Cargo.toml'])
    if frontend: paths.add(frontend)
    return [dict(artifact(path),executable=path.resolve() in executables) for path in sorted(paths)]


def save(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.report-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(value,out,indent=2,allow_nan=False)
            out.write('\n')
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def environment():
    import importlib.metadata
    import platform
    versions = {}
    for package in ['psycopg','psycopg-binary','pyte','wcwidth']:
        try: versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError: versions[package] = None
    return {'python':platform.python_version(),'system':platform.system(),'kernel':platform.release(),'packages':versions}
