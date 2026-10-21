#!/usr/bin/env python3
"""Current-main terminal and driver gates. Only a marked disposable fixture is allowed.

Adapted from codex_codex_postgres_writable's ui_acceptance.py and the Python
read-ticket/cancel design in codex_python_quotes_neovim. No sibling paths/imports.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'benchmarks/quotes_perf'))
from terminal import Terminal

CASES = ['insert', 'update', 'delete', 'edit_cancel', 'bulk', 'bulk_rollback',
         'readonly_controls', 'raw_write_guard', 'generated_edit', 'advanced_arrays_edit',
         'json_null_to_sql_null', 'sql_null_to_json_null', 'driver_cancel', 'stale_cancel',
         'queued_cancel', 'unrelated_cancel', 'write_not_cancelled', 'read_cancel', 'superseded_read', 'read_deadline']


def fixture(path):
    assert not path.is_symlink() and path.stat().st_mode & 0o077 == 0, 'descriptor must be private'
    data = json.loads(path.read_text())
    assert data['fixture_marker'] == 'dbcli-postgres-write-disposable-v1'
    assert data['database'] == 'dbcli_postgres_write_fixture'
    info = json.loads(subprocess.check_output(['docker', 'inspect', data['container']]))[0]
    assert info['Id'] == data['container_id']
    assert info['Config']['Labels'].get('dbcli.fixture') == 'postgres-write'
    assert any(b['HostIp'] == '127.0.0.1' and int(b['HostPort']) == data['port']
               for b in info['NetworkSettings']['Ports']['5432/tcp'])
    for key in ['url', 'admin_url']:
        u = urlsplit(data[key])
        assert u.scheme in ['postgres', 'postgresql'] and u.hostname == '127.0.0.1'
        assert u.port == data['port'] and u.port != 5432
        assert u.path == '/dbcli_postgres_write_fixture'
    with psycopg.connect(data['admin_url']) as c:
        assert c.execute('SELECT current_database()').fetchone()[0] == data['database']
    return data


def tagged_url(url, name):
    u = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(u.query) if k != 'application_name']
    return urlunsplit(u._replace(query=urlencode(query + [('application_name', name)])))


def wait(predicate, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('condition timed out')


@contextmanager
def owned_schema(data):
    name = f'ui_accept_{os.getpid()}_{time.time_ns()}'
    with psycopg.connect(data['admin_url'], autocommit=True) as c:
        c.execute(f'CREATE SCHEMA {name}')
        try:
            yield c, name
        finally:
            c.execute(f'DROP SCHEMA {name} CASCADE')


def sleeping(c, tag):
    row = c.execute("SELECT pid FROM pg_stat_activity WHERE application_name=%s AND wait_event='PgSleep'", (tag,)).fetchone()
    return row[0] if row else None


def cancelled_log(data, pid):
    logs = subprocess.run(['docker', 'logs', data['container']], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=5, check=True)
    return any(f'[{pid}]' in line and 'canceling statement due to user request' in line
               for line in (logs.stdout + logs.stderr).decode(errors='replace').splitlines())


class Protocol:
    def __init__(self, worker, data, tag):
        self.socket, remote = socket.socketpair()
        self.socket.settimeout(2)
        env = dict(os.environ, DBCLI_POSTGRES_URL=tagged_url(data['url'], tag),
                   DBCLI_WORKER_CANCEL_FD=str(remote.fileno()))
        self.process = subprocess.Popen([str(worker), '--postgres', '--protocol'], env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, pass_fds=(remote.fileno(),))
        remote.close()

    def send(self, id, **op):
        self.process.stdin.write((json.dumps(dict(op, _request_id=id)) + '\n').encode())
        self.process.stdin.flush()

    def result(self):
        assert select.select([self.process.stdout], [], [], 5)[0], 'worker response timed out'
        return json.loads(self.process.stdout.readline())

    def cancel(self, id):
        self.socket.sendall(struct.pack('<Q', id))
        frame = b''
        while len(frame) < 8:
            chunk = self.socket.recv(8 - len(frame))
            assert chunk
            frame += chunk
        assert struct.unpack('<Q', frame)[0] == id

    def close(self):
        self.process.kill()
        self.process.wait(timeout=3)
        self.socket.close()


def driver_case(action, args, data):
    tag = f'driver_{os.getpid()}_{time.time_ns()}'
    with owned_schema(data) as (c, schema):
        p = Protocol(args.worker, data, tag)
        try:
            if action == 'queued_cancel':
                p.cancel(1)
                p.send(1, op='raw', sql='SELECT 1 AS n FROM pg_sleep(2.8)', limit=1, offset=0)
                assert p.result()['error']['class'] == 'ReadCancelledError'
            elif action == 'write_not_cancelled':
                c.execute(f'CREATE TABLE {schema}.items(id INT PRIMARY KEY, name TEXT)')
                c.execute(f"INSERT INTO {schema}.items VALUES(1,'before')")
                c.execute(f"CREATE FUNCTION {schema}.slow() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN PERFORM pg_sleep(1); RETURN NEW; END$$")
                c.execute(f'CREATE TRIGGER slow BEFORE UPDATE ON {schema}.items FOR EACH ROW EXECUTE FUNCTION {schema}.slow()')
                p.send(1, op='apply_batch', table=f'{schema}.items', actions=[
                    {'kind': 'update', 'original': {'id': 1, 'name': 'before'}, 'row': {'id': 1, 'name': 'after'}}])
                wait(lambda: sleeping(c, tag))
                p.cancel(1)
                assert p.result()['ok'] is True, 'read cancellation interrupted a write'
                assert c.execute(f'SELECT name FROM {schema}.items').fetchall() == [('after',)]
            else:
                if action == 'stale_cancel':
                    p.send(1, op='raw', sql='SELECT 1 AS n', limit=1, offset=0)
                    assert p.result()['ok']
                id = 2 if action == 'stale_cancel' else 1
                p.send(id, op='raw', sql='SELECT 7 AS n FROM pg_sleep(2.5)', limit=1, offset=0)
                pid = wait(lambda: sleeping(c, tag))
                start = time.monotonic()
                p.cancel(99 if action == 'unrelated_cancel' else 1)
                result = p.result()
                if action == 'driver_cancel':
                    assert not result['ok'] and '57014' in result['error']['message'], 'driver did not cancel server statement'
                    assert time.monotonic() - start < 1.5, 'cancellation waited for server timeout'
                    assert cancelled_log(data, pid)
                else:
                    assert result['ok'] and result['result']['rows'] == [[7]], 'stale ID cancelled a newer read'
            # Worker remains alive after native cancellation; a new read still works.
            p.send(3, op='raw', sql='SELECT 42 AS n', limit=1, offset=0)
            assert p.result()['result']['rows'] == [[42]]
        finally:
            p.close()


def snapshot(c, table):
    return c.execute(f"SELECT id,name,code,payload::text,plain_json::text,encode(data,'hex'),base,computed,"
                     f'payload IS NULL,plain_json IS NULL,uids::text,amounts::text,jsons::text,'
                     f'array_dims(jsons) FROM {table} ORDER BY id').fetchall()


def editor(folder):
    control, log, script = folder/'content.json', folder/'called', folder/'editor.py'
    control.write_text('{}')
    script.write_text('''import json,os,sys
from pathlib import Path
control=json.loads(Path(os.environ['DBCLI_ACCEPT_EDITOR']).read_text())
path=Path(sys.argv[-1])
if 'text' in control: path.write_text(control['text'])
else:
 value=json.loads(path.read_text())
 if 'updates' in value:
  value['updates'][0]['row'].update(control['patch'])
  value['insert']=control.get('insert',[])
 else: value.update(control['patch'])
 path.write_text(json.dumps(value))
with Path(os.environ['DBCLI_ACCEPT_EDITOR_LOG']).open('a') as f:f.write('called\\n')
''')
    return control, log, shlex.join([sys.executable, str(script)])


def ui_case(action, args, data):
    with owned_schema(data) as (c, schema), tempfile.TemporaryDirectory(prefix='dbcli-ui-accept-') as tmp:
        folder = Path(tmp)
        table = f'{schema}.items'
        c.execute(f'CREATE TABLE {table}(id INT PRIMARY KEY,name TEXT NOT NULL,code TEXT,payload JSONB,plain_json JSON,data BYTEA,base INT DEFAULT 3,computed INT GENERATED ALWAYS AS(base*2) STORED,uids UUID[],amounts NUMERIC[],jsons JSONB[])')
        c.execute(f"INSERT INTO {table}(id,name,code,payload,plain_json,data) VALUES(1,'alpha','00123','null','null',decode('00ff','hex')),(2,'beta','00234',NULL,NULL,decode('','hex'))")
        if action == 'sql_null_to_json_null':
            c.execute(f'UPDATE {table} SET payload=NULL,plain_json=NULL WHERE id=1')
        if action == 'advanced_arrays_edit':
            c.execute(f"UPDATE {table} SET uids=ARRAY['00000000-0000-0000-0000-000000000001'::uuid,NULL],amounts=ARRAY[1234567890123456789.12345678901234567890::numeric,NULL],jsons=ARRAY[NULL,'null'::jsonb,'{{\"$bytes\":\"not-hex\"}}'::jsonb] WHERE id=1")
        if action == 'generated_edit':
            c.execute(f'ALTER TABLE {table} ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY')
        before = snapshot(c, table)
        control, log, cmd = editor(folder)
        tag = f'ui_{os.getpid()}_{time.time_ns()}'
        env = dict(os.environ, DBCLI_POSTGRES_URL=tagged_url(data['url'], tag), TERM='xterm-256color',
                   EDITOR=cmd, DBCLI_BULK_EDITOR=cmd, DBCLI_ACCEPT_EDITOR=str(control),
                   DBCLI_ACCEPT_EDITOR_LOG=str(log))
        if action == 'read_deadline': env['DBCLI_WORKER_DEADLINE_MS'] = '500'
        command = [str(args.frontend), '--engine', 'postgres', '--backend', 'original-rust',
                   '--worker', str(args.worker), '--table', table]
        if action == 'readonly_controls': command += ['--read-only']
        t = Terminal(command, folder, env, columns=160)
        def ready(): return 'alpha' in t.display and 'BUSY' not in t.display
        def external(value):
            control.write_text(json.dumps(value)); n = log.stat().st_size if log.exists() else 0
            t.send(b'\x07')
            assert t.wait(lambda: log.exists() and log.stat().st_size > n), 'external editor not called'
        def sql(text):
            t.send(':'); external({'text': text}); t.send(b'\x12')
        try:
            assert t.wait(ready), 'initial UI rows missing'
            # An UPDATE can move a fixture tuple; choose deterministic PK order.
            t.send('o')
            assert t.wait(lambda: ready() and 'id ↑' in t.display), 'PK sort not loaded'
            if action in ['read_cancel', 'superseded_read', 'read_deadline']:
                sql('SELECT 19 AS result FROM pg_sleep(2.8)')
                pid = wait(lambda: sleeping(c, tag))
                if action == 'read_cancel':
                    t.send(b'\x1b'); assert t.wait(lambda: 'Read cancelled' in t.display)
                elif action == 'superseded_read': sql('SELECT 42 AS fresh_result')
                else: assert t.wait(lambda: 'deadline exceeded' in t.display)
                assert wait(lambda: cancelled_log(data, pid), 2), 'UI did not send driver cancellation'
                if action != 'superseded_read': sql('SELECT 42 AS fresh_result')
                assert t.wait(lambda: 'fresh_result' in t.display and '42' in t.display and 'BUSY' not in t.display)
                t.drain(.2)
                assert 'fresh_result' in t.display and t.process.poll() is None
            elif action == 'readonly_controls':
                for key in ['a', 'e', 'E', 'dd']:
                    t.send(key)
                    assert 'Read only' in t.display
                assert not log.exists() and snapshot(c, table) == before
            elif action == 'raw_write_guard':
                c.execute(f"CREATE FUNCTION {schema}.write_probe() RETURNS INT LANGUAGE plpgsql AS $$BEGIN INSERT INTO {table}(id,name) VALUES(999,'forbidden'); RETURN 1; END$$")
                sql(f'SELECT {schema}.write_probe()')
                assert t.wait(lambda: 'worker rejected operation' in t.display)
                assert snapshot(c, table) == before
            else:
                if action == 'delete':
                    t.send('d'); t.send('d'); assert t.wait(lambda: 'DELETE' in t.display)
                    t.send('DELETE'); t.send('y')
                    assert t.wait(lambda: c.execute(f'SELECT count(*) FROM {table} WHERE id=1').fetchone()[0] == 0)
                    assert snapshot(c, table) == before[1:]
                else:
                    if action == 'insert':
                        value = {'text': json.dumps({'id':900,'name':'inserted','code':'00900'})}
                    else:
                        patch = {'name':'edited'}
                        if action == 'generated_edit': patch['base'] = 8
                        if action in ['json_null_to_sql_null', 'sql_null_to_json_null']:
                            patch.update(payload=None if action == 'json_null_to_sql_null' else {'$type':'json','value':None},
                                         plain_json=None if action == 'json_null_to_sql_null' else {'$type':'json','value':None})
                        value = {'patch': patch}
                        if action in ['bulk', 'bulk_rollback']:
                            value['insert'] = [{'id':901 if action == 'bulk' else 2,'name':'batch inserted'}]
                    if action in ['bulk', 'bulk_rollback']:
                        control.write_text(json.dumps(value)); t.send('E')
                        assert t.wait(lambda: 'bulk count confirmation' in t.display.lower()), 'bulk count confirmation missing'
                    else:
                        t.send('a' if action == 'insert' else 'e'); external(value)
                        t.send(b'\x12'); assert t.wait(lambda: 'preview' in t.display.lower()), 'mutation preview missing'
                    if action == 'edit_cancel':
                        t.send(b'\x1b'); assert snapshot(c, table) == before
                    else:
                        t.send('y')
                        if action == 'bulk_rollback':
                            assert t.wait(lambda: 'Write not confirmed' in t.display)
                            assert snapshot(c, table) == before, 'failed batch changed data'
                        else:
                            if action == 'insert':
                                assert t.wait(lambda: len(snapshot(c,table)) == 3)
                                after = snapshot(c,table)
                                assert after[:2] == before
                                assert after[2] == (900,'inserted','00900',None,None,None,3,6,True,True,None,None,None,None)
                            else:
                                assert t.wait(lambda: snapshot(c,table)[0][1] == 'edited')
                                expected = [list(r) for r in before]; expected[0][1] = 'edited'
                                if action == 'generated_edit': expected[0][6:8] = [8,16]
                                if action in ['json_null_to_sql_null', 'sql_null_to_json_null']:
                                    null = action == 'json_null_to_sql_null'
                                    expected[0][3:5] = [None,None] if null else ['null','null']
                                    expected[0][8:10] = [null,null]
                                if action == 'bulk': expected.append([901,'batch inserted',None,None,None,None,3,6,True,True,None,None,None,None])
                                assert snapshot(c,table) == [tuple(r) for r in expected], 'collateral typed-row change'
        except Exception:
            capture = ROOT/'.local/postgres-write/acceptance-failure.txt'
            capture.write_text(t.display)
            capture.chmod(0o600)
            raise
        finally:
            lifecycle = t.close()
            assert lifecycle['exit_code'] == 0 and not lifecycle['forced_exit'], 'UI failed normal exit'
            assert lifecycle['termios_restored'] and lifecycle['alternate_screen_restored'], 'terminal not restored'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fixture', type=Path, required=True)
    p.add_argument('--worker', type=Path, default=ROOT/'rust/target/release/dbcli-rust')
    p.add_argument('--frontend', type=Path, default=ROOT/'ui/rust/target/release/dbcli-ui-rust')
    p.add_argument('--cases', nargs='+', choices=CASES, default=CASES)
    args = p.parse_args(); args.worker = args.worker.resolve(); args.frontend = args.frontend.resolve()
    data = fixture(args.fixture)
    passed = []
    for action in args.cases:
        # Failure output stays categorical: never print SQL rows or descriptor URLs.
        try:
            (driver_case if action in ['driver_cancel','stale_cancel','queued_cancel','unrelated_cancel','write_not_cancelled'] else ui_case)(action,args,data)
        except Exception as error:
            print(json.dumps({'failed': action, 'error': type(error).__name__, 'reason': str(error) if isinstance(error, AssertionError) else 'details suppressed', 'line': traceback.extract_tb(error.__traceback__)[-1].lineno}), flush=True)
            return 1
        passed.append(action); print(json.dumps({'passed': action}), flush=True)
    print(json.dumps({'passed_cases': passed, 'count': len(passed), 'fixture_only': True}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
