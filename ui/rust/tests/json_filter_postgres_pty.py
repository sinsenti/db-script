#!/usr/bin/env python3
"""Read-only JSON filter acceptance on an explicitly selected PostgreSQL copy.

Requires psycopg and pyte (benchmarks/quotes_perf/requirements.txt). No schema,
data, export or editor writes are performed. Credentials remain in memory/env.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from psycopg import sql

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'benchmarks/quotes_perf'))
from terminal import Terminal
from dbcli.postgres_target import connection_factory, docker_target, target_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', required=True)
    args = parser.parse_args()
    url, _ = docker_target(args.container, read_only=True)
    env = dict(target_environment(url, read_only=True), TERM='xterm-256color')
    worker = ROOT / 'rust/target/release/dbcli-rust'
    frontend = ROOT / 'ui/rust/target/release/dbcli-ui-rust'
    passed = []
    with connection_factory(url, read_only=True)() as c:
        assert c.execute("SELECT current_setting('transaction_read_only')").fetchone() == ('on',)
        def snapshot():
            return c.execute("SELECT count(*),md5(string_agg(md5(to_jsonb(q)::text),'' ORDER BY id::text)) FROM public.quotes q").fetchone()
        before = snapshot()
        first = c.execute('SELECT extracted_spec FROM public.quotes LIMIT 1').fetchone()[0]
        assert isinstance(first, dict), 'First loaded row must have a JSON object'
        command = [str(frontend), '--engine', 'postgres', '--backend', 'original-rust',
                   '--worker', str(worker), '--table', 'public.quotes', '--read-only']
        t = Terminal(command, ROOT, env, columns=200, rows=44)
        def prompt():
            return 'Filter · Ctrl+R apply' in t.display
        def loaded(count):
            return not prompt() and 'BUSY' not in t.display and re.search(rf'\b{count} rows ·', t.display)
        def open_filter():
            t.send('/')
            assert t.wait(prompt), 'Filter prompt did not open'
            t.send(b'\x15')  # Ctrl+U clears the previous filter.
        def choose_field(key, prefix=''):
            open_filter()
            t.send(prefix + 'extracted_spec')
            assert 'JSON column' in t.display, 'JSON column absent'
            t.send('\r')
            assert prompt() and 'JSON path' in t.display, 'Choosing column closed filter'
            t.send(key)
            t.send('\r')
            assert prompt() and '#>>' in t.display, 'Choosing leaf closed filter'
            assert 'predicate' in t.display, 'Predicate suggestions absent'
        def verify(predicate, name, submit=b'\x12'):
            expected = [str(r[0]) for r in c.execute(
                'SELECT id FROM public.quotes WHERE ' + predicate + ' ORDER BY id')]
            t.send(submit)
            assert t.wait(lambda: loaded(len(expected))), f'{name}: UI row count disagrees with SQL'
            # Verify both backend paging and count with the identical predicate.
            requests = [dict(op='page', table='public.quotes', filter=predicate,
                             sort=['id', 'ASC'], limit=100000, offset=0),
                        dict(op='count', table='public.quotes', filter=predicate)]
            result = subprocess.run([str(worker), '--postgres', '--protocol', '--read-only'],
                                    env=env, input=''.join(json.dumps(r)+'\n' for r in requests),
                                    capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, 'Read worker failed'
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            assert len(replies) == 2 and all(r['ok'] for r in replies), 'Worker rejected read'
            page = replies[0]['result']
            actual = []
            for row in page['rows']:
                value = row[page['headers'].index('id')]
                if isinstance(value, dict) and value.get('$type') == 'uuid':
                    value = value['value']
                actual.append(str(value))
            assert actual == expected and replies[1]['result'] == len(expected), f'{name}: backend result mismatch'
            passed.append({'case': name, 'matching_rows': len(expected), 'ids_match_sql': True})
        try:
            assert t.wait(lambda: loaded(before[0])), 'Initial quotes page failed to load'
            assert 'Tables focused' in t.display
            t.send('\t')
            # Accept a cached text value, then Enter applies the finished filter.
            choose_field('manufacturer')
            t.send('\t')  # First predicate is =.
            assert 'JSON value' in t.display
            t.send('\r')
            assert prompt(), 'Choosing cached value closed filter'
            literal = sql.Literal(str(first['manufacturer'])).as_string(c)
            verify(f'(\"extracted_spec\" #>> ARRAY[\'manufacturer\']) = {literal}', 'cached_text_enter', b'\r')

            # Restore the original page before sampling another cached value.
            open_filter(); t.send(b'\x12')
            assert t.wait(lambda: loaded(before[0]))
            key = next(k for k, v in first.items() if isinstance(v, (int, float)) and not isinstance(v, bool))
            choose_field(key)
            t.send('\t'); t.send('\r')
            literal = sql.Literal(str(first[key])).as_string(c)
            verify(sql.SQL('({} #>> ARRAY[{}]) = {}').format(sql.Identifier('extracted_spec'), sql.Literal(key), sql.SQL(literal)).as_string(c), 'cached_numeric_text')

            choose_field('condition')
            t.send('IS NULL'); t.send('\t')
            verify('(\"extracted_spec\" #>> ARRAY[\'condition\']) IS NULL', 'null_predicate')

            choose_field('model', 'id IS NOT NULL AND ')
            t.send('IS NOT NULL'); t.send('\t')
            verify('id IS NOT NULL AND (\"extracted_spec\" #>> ARRAY[\'model\']) IS NOT NULL', 'combined_clause')

            open_filter()
            t.send('\"extracted_spec\".manufacturer')
            t.send('\t'); t.send('IS NOT NULL'); t.send('\t')
            verify('(\"extracted_spec\" #>> ARRAY[\'manufacturer\']) IS NOT NULL', 'quoted_prefix_tab')

            # Esc hides suggestions first, then closes without applying a draft.
            open_filter(); t.send('extracted_spec'); t.send('\r')
            assert prompt() and 'JSON path' in t.display
            t.send(b'\x1b'); assert prompt() and 'Suggestions ·' not in t.display
            t.send('\t'); assert prompt() and 'Suggestions ·' in t.display
            t.send(b'\x1b'); t.send(b'\x1b'); assert not prompt()
            passed.append({'case': 'escape_reopen_cancel', 'passed': True})
        finally:
            lifecycle = t.close()
        assert lifecycle == dict(exit_code=0, forced_exit=False, termios_restored=True, alternate_screen_restored=True)
        # READ COMMITTED gets a fresh snapshot here, detecting collateral changes.
        assert snapshot() == before, 'Quotes snapshot changed during verification'
    print(json.dumps(dict(container=args.container, table='public.quotes', column='extracted_spec',
                          read_only=True, quotes_unchanged=True, passed=passed, lifecycle=lifecycle), indent=2))


if __name__ == '__main__':
    main()
