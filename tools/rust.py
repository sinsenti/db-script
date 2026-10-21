#!/usr/bin/env python3
"""Launch the selected Original Rust backend and Ratatui interface from this checkout."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import shlex
import sys
from postgres_target import DEFAULT_CONTAINER,TargetError,docker_target,target_environment

from dbcli.rust import ROOT, WORKER, FRONTEND, command

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    target=parser.add_mutually_exclusive_group()
    target.add_argument('--database',help='existing SQLite file')
    target.add_argument('--postgres',action='store_true',help='PostgreSQL; writable by default')
    source=parser.add_mutually_exclusive_group()
    source.add_argument('--container',help='PostgreSQL container (default: '+DEFAULT_CONTAINER+')')
    source.add_argument('--from-env',action='store_true',help='use DBCLI_POSTGRES_URL without Docker')
    parser.add_argument('--table');parser.add_argument('--read-only',action='store_true')
    parser.add_argument('--legacy',action='store_true',help='use the backend’s original terminal interface')
    parser.add_argument('--print-command',action='store_true',help='show native argv without resolving credentials')
    parser.add_argument('--list',action='store_true',help='show local executable availability')
    args=parser.parse_args(argv)
    if args.list:
        for name,path in [('backend',WORKER),('frontend',FRONTEND)]:print(name,'built'if path.is_file()else'not built',path)
        return 0
    if not args.postgres and(args.container or args.from_env):parser.error('--container and --from-env require --postgres')
    try:cmd=command(database=args.database,postgres=args.postgres,table=args.table,read_only=args.read_only,legacy=args.legacy)
    except ValueError as error:parser.error(str(error))
    if args.print_command:print(shlex.join(cmd));return 0
    if not WORKER.is_file()or(not args.legacy and not FRONTEND.is_file()):parser.error('Run bash tools/build_rust.sh first')
    try:
        env=dict(os.environ)
        if args.postgres:
            url=os.environ.get('DBCLI_POSTGRES_URL','')if args.from_env else docker_target(args.container or DEFAULT_CONTAINER,read_only=args.read_only)[0]
            env=target_environment(url,read_only=args.read_only)
        os.execve(cmd[0],cmd,env)
    except TargetError as error:print(str(error),file=sys.stderr);return 1
    except OSError:print('Could not start the Rust browser',file=sys.stderr);return 1
    return 0
if __name__=='__main__':raise SystemExit(main())
