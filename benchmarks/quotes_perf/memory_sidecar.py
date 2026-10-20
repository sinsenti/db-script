#!/usr/bin/env python3
"""Read /proc only for the running quotes UI benchmark; no SQL or UI input."""
from __future__ import annotations
import argparse,hashlib,json,os,time
from pathlib import Path


def children(pid):
    found=set()
    for path in Path('/proc',str(pid),'task').glob('*/children'):
        try:found.update(int(v)for v in path.read_text().split())
        except OSError:pass
    return found


def tree(pid):
    seen={pid};todo=[pid]
    while todo:
        for child in children(todo.pop()):
            if child not in seen:seen.add(child);todo.append(child)
    return seen


def process(pid,ui_pid):
    try:
        directory=Path('/proc',str(pid))
        args=directory.joinpath('cmdline').read_bytes().split(b'\0')
        if any(b'nvim_probe.py'in arg for arg in args):return None
        status=directory.joinpath('status').read_text();stat=directory.joinpath('stat').read_text().rsplit(')',1)[1].split()
        memory={line.split(':',1)[0]:int(line.split()[1])for line in status.splitlines()if line.startswith(('VmRSS:','VmHWM:'))}
        executable=directory.joinpath('exe').resolve().name
        return {'pid':pid,'parent_pid':int(stat[1]),'start_ticks':int(stat[19]),'role':'ui'if pid==ui_pid else 'nvim'if executable=='nvim'else 'worker',
                'executable':executable,'cpu_seconds':(int(stat[11])+int(stat[12]))/os.sysconf('SC_CLK_TCK'),
                'rss_kib':memory.get('VmRSS',0),'peak_rss_kib':memory.get('VmHWM',0)}
    except (OSError,ValueError,IndexError):return None


class Sampler:
    """Observed process-tree peaks; exited processes retain their last CPU sample."""
    def __init__(self, pid):
        import threading
        self.pid = pid
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.peaks = {}
        self.peak_rss_kib = 0
        self.samples = 0
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def snapshot(self):
        records = [r for pid in tree(self.pid) if (r := process(pid,self.pid)) is not None]
        with self.lock:
            self.samples += 1
            self.peak_rss_kib = max(self.peak_rss_kib, sum(r['rss_kib'] for r in records))
            for row in records:
                key = (row['pid'],row['start_ticks'])
                self.peaks[key] = max(self.peaks.get(key,0),row['cpu_seconds'])
            return {'rss_kib':sum(r['rss_kib']for r in records), 'cpu_observed_seconds':sum(self.peaks.values()), 'sampled_peak_rss_kib':self.peak_rss_kib, 'samples':self.samples, 'processes':len(records), 'nvim_present':any(r['role']=='nvim'for r in records)}

    def run(self):
        while not self.stop.is_set():
            self.snapshot()
            self.stop.wait(.02)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        return self.snapshot()
