"""Read-only JSONL clients. Native protocol shapes match the retained UI bridge."""
from __future__ import annotations
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
from common import exact_loads, exact_dumps


class Runner:
    def __init__(self, candidate, env):
        self.kind = candidate['protocol']
        self.serial = 0
        self.schema = __import__('common').table_parts(env['DBCLI_PERF_TABLE'])[0]
        self.pending = bytearray()
        if self.kind == 'python-reference':
            command = [sys.executable,str(Path(__file__).with_name('python_worker.py'))]
        else:
            flags = {'dispatch':['--protocol'],'batch':['--runner'],'rewrite':['--protocol=jsonl']}[self.kind]
            command = [candidate['worker'],'--postgres',*flags,'--read-only']
        self.proc = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout,selectors.EVENT_READ)
        try:
            if self.kind == 'rewrite' and self.line().get('ready') != 1:
                raise RuntimeError('protocol_not_ready')
        except BaseException:
            self.close()
            raise

    def line(self):
        deadline = time.monotonic()+30
        while b'\n' not in self.pending:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining): raise TimeoutError('read_timeout')
            chunk = os.read(self.proc.stdout.fileno(),65536)
            if not chunk: raise RuntimeError('worker_exited')
            self.pending.extend(chunk)
            if len(self.pending) > 128*1024*1024: raise RuntimeError('response_exceeds_128_MiB')
        raw, _, remainder = self.pending.partition(b'\n')
        self.pending = bytearray(remainder)
        return exact_loads(raw)

    def request(self, operation):
        self.serial += 1
        op = dict(operation)
        if op['op'] not in {'tables','page','count','schema','raw'}: raise ValueError('Only benchmark reads are supported')
        if self.kind == 'batch':
            request = {'engine':'postgres','database':'postgres','read_only':True,'operations':[op]}
        elif self.kind == 'dispatch':
            request = dict(op)
            if op['op'] == 'tables': request['op'] = 'list_tables'
            if 'order_by' in request: request['sort'] = request.pop('order_by')
        elif self.kind == 'rewrite':
            op_name = op.pop('op')
            if 'order_by' in op:
                key,direction = op['order_by'];op['order_by'] = {'column':key,'direction':direction}
            request = {'id':self.serial,'op':{'tables':'list_tables','page':'fetch_page','count':'count_rows','raw':'execute_readonly'}.get(op_name,op_name),'args':op}
        else: request = op
        self.proc.stdin.write(exact_dumps(request).encode()+b'\n');self.proc.stdin.flush()
        result = self.line()
        if self.kind == 'batch':
            result = result['results'][0]
            return {'ok':result['ok'],**({'value':result['value']} if result['ok'] else {'error':'worker_rejected_read'})}
        if self.kind == 'python-reference': return result
        value = result.get('result')
        if operation['op'] == 'tables' and result.get('ok'):
            value = [name for name in value if name.startswith(self.schema+'.')]
        return {'ok':result.get('ok',False),**({'value':value} if result.get('ok') else {'error':'worker_rejected_read'})}

    def close(self):
        if not self.proc.stdin.closed: self.proc.stdin.close()
        try: self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired: self.proc.kill();self.proc.wait()
        self.proc.stdout.close();self.selector.close()
