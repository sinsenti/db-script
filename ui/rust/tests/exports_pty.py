#!/usr/bin/env python3
"""Existing-file refusal and retry in real Rust UIs; temporary SQLite only."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from bulk_neovim_pty import Terminal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary',type=Path)
    parser.add_argument('--kind',choices=['original','ratatui'],required=True)
    parser.add_argument('--worker',type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='dbcli-export-pty-') as folder:
        folder = Path(folder);database = folder/'fixture.sqlite'
        with sqlite3.connect(database) as con:
            con.executescript("CREATE TABLE items(id INTEGER PRIMARY KEY,name TEXT);INSERT INTO items VALUES(1,'alpha');")
        before = hashlib.sha256(database.read_bytes()).digest()
        existing = folder/('items_export.json' if args.kind=='original' else 'export.json')
        existing.write_bytes(b'preserve existing export')
        fresh = folder/'fresh.json'
        command = [str(args.binary.resolve()),'--read-only']
        if args.kind=='original':command += ['--db',str(database),'--table','items']
        else:command += ['--engine','sqlite','--database',str(database),'--table','items','--backend','original-rust','--worker',str(args.worker.resolve())]
        terminal = Terminal(command,folder);terminal.kind = args.kind
        try:
            assert terminal.wait(lambda:b'alpha' in terminal.output),'initial rows missing'
            terminal.send('x')
            if args.kind=='original':
                assert terminal.wait(lambda:b'File exists. New export name' in terminal.output),'overwrite did not offer a new name'
                assert existing.read_bytes()==b'preserve existing export'
                terminal.send('fresh\r')
            else:
                assert terminal.wait(lambda:b'export.json' in terminal.output),'export path input missing'
                terminal.send('\r')
                assert terminal.wait(lambda:b'already exists' in terminal.output),'existing destination not refused'
                assert existing.read_bytes()==b'preserve existing export'
                terminal.send('\x15fresh.json\r')
            assert terminal.wait(fresh.exists),'new export path was not accepted'
            assert existing.read_bytes()==b'preserve existing export'
            assert json.loads(fresh.read_text())==[{'id':1,'name':'alpha'}]
            assert fresh.stat().st_mode & 0o777 == 0o600
            if args.kind=='original':terminal.send('\r')
        finally:terminal.close()
        assert before==hashlib.sha256(database.read_bytes()).digest(),'export modified database'
    print(json.dumps({'kind':args.kind,'existing_file_preserved':True,'retry_export_passed':True,'terminal_restored':True}))
if __name__=='__main__':raise SystemExit(main())
