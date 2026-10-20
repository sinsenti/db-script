#!/usr/bin/env python3
"""Benchmark main's Ratatui/legacy interfaces and real Neovim without applying edits."""
from __future__ import annotations
import json
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from common import target, metadata, target_environment, digest_result
from config import parser, candidates, provenance, save, PROTOCOL_ALIASES, environment, artifact
from client import Runner
from backend_bench import workloads, oracle
from readonly_pg_proxy import Guard
from terminal import Terminal
from memory_sidecar import Sampler


def command(candidate, kind, args):
    if kind == 'legacy':
        return [candidate['worker'],'--postgres','--table',args.table]
    alias = candidate.get('backend',PROTOCOL_ALIASES[candidate['protocol']])
    return [str(args.frontend.resolve()),'--engine','postgres','--backend',alias,'--worker',candidate['worker'],'--table',args.table]

ANSI=re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')
COUNTER=re.compile(r'(?:Rows\s+1\s*[–−-]\s*[1-9]|\b1[–−-][1-9][0-9]*\s*/\s*[1-9])',re.I)


class InitialFrame:
    def __init__(self,terminal,kind,table='public.quotes'):self.terminal=terminal;self.kind=kind;self.table=table;self.signature=None;self.changed=time.monotonic()
    def __call__(self):
        text=self.terminal.display;content=self.terminal.content
        signature=len(content)if self.kind=='legacy'else text
        if signature!=self.signature:self.signature=signature;self.changed=time.monotonic()
        if self.kind=='legacy':
            # Legacy renderers can overflow/scroll their title away on wide
            # multiline data. Only source-defined header metadata is inspected
            # in the in-memory stream; no screen or row values are saved.
            raw=ANSI.sub('',content.decode('utf-8','replace'))
            found=bool(re.search(r'(?:Data Browser:[^\r\n]*'+re.escape(self.table)+r'|(?:^|[\r\n])\s*'+re.escape(self.table)+r')[^\r\n]*Rows\s+1\s*[–−-]\s*[1-9]',raw,re.I))
        else:
            lines=text.splitlines();bands='\n'.join(lines[:3]+lines[-3:])
            found=self.table in text and bool(COUNTER.search(text))and'BUSY'not in bands and'Loading'not in bands
        return found and time.monotonic()-self.changed>=.15
    def diagnostics(self):
        text=self.terminal.display
        return {'table_header_visible':self.table in text,'counter_visible':bool(COUNTER.search(text)),
                'legacy_header_scrolled_out':self.kind=='legacy'and(self.table not in text or not COUNTER.search(text)),
                'output_bytes':len(self.terminal.content)}


def graceful_close(terminal,kind):
    """Escape the returned JSON/modal first; q alone would edit its document."""
    started=time.monotonic_ns()
    if terminal.process.poll()is None:
        if kind=='legacy':
            text=terminal.display.lower()
            if 'press any key'in text or 'press enter'in text:terminal.send('\r',.1)
            else:terminal.send(b'\x1b',.1)
            # Separate keys avoid the frozen escape decoder consuming Ctrl+C
            # as the second byte of an Escape sequence. Four back steps cover
            # detail, browser and the explicit target's enclosing table menus.
            for _ in range(4):
                if terminal.process.poll()is not None:break
                terminal.send('q',.1)
            terminal.wait(lambda:terminal.process.poll()is not None,3)
        else:
            terminal.send(b'\x1b',.1)
            terminal.send(b'\x03',.1)
            terminal.wait(lambda:terminal.process.poll()is not None,10)
    lifecycle=terminal.close();lifecycle['cleanup_ms']=(time.monotonic_ns()-started)/1e6
    content=terminal.content
    lifecycle['alternate_screen_restored']=content.rfind(b'\x1b[?1049l')>content.rfind(b'\x1b[?1049h')
    return lifecycle


def lifecycle_ok(value):
    return value.get('exit_code')==0 and not value.get('forced_exit')and value.get('termios_restored')and value.get('alternate_screen_restored')


def events(path):
    if not path.exists():return []
    try:return[json.loads(v)for v in path.read_text().splitlines()]
    except (OSError,ValueError):return []

def one(candidate,kind,url,args):
    result={'implementation':candidate['name'],'interface':kind,'timing_status':'ERROR'};terminal=None;sampler=None
    with tempfile.TemporaryDirectory(prefix='dbcli-quotes-perf-')as temp,Guard(url)as guard:
        folder=Path(temp);os.chmod(folder,0o700);log=folder/'events';log.touch(mode=0o600);script=folder/'probe.lua'
        script.write_text('''local function emit(value)
 vim.fn.writefile({vim.json.encode(value)},vim.env.DBCLI_QUOTE_EVENTS,'a')
end
local function cancel()vim.cmd('cquit 23')end
vim.api.nvim_create_autocmd('VimEnter',{once=true,callback=function()
 vim.schedule(function()
  vim.cmd('redraw!')
  emit({phase='nvim_ready',time_ns=string.format('%.0f',vim.uv.hrtime()),line_count=vim.api.nvim_buf_line_count(0),diff=vim.wo.diff})
  if not vim.wo.diff then
   emit({phase='diff_probe',status='NOT_APPLICABLE'})
   vim.defer_fn(cancel,500)
   return
  end
  -- Delay only the secondary probe, after the initial-ready timestamp/sample.
  vim.defer_fn(function()
   local ok=pcall(function()
    assert(vim.bo.modifiable and not vim.bo.readonly)
    local target=nil
    for n,line in ipairs(vim.api.nvim_buf_get_lines(0,0,-1,false))do
     if line:match('"row"%s*:')then target=n;break end
    end
    assert(target)
    local before=vim.api.nvim_buf_get_lines(0,target-1,target,false)[1]
    local started=vim.uv.hrtime()
    -- JSON whitespace changes one editable-buffer line, never the original.
    -- No file is saved, and the session always exits :cq without confirmation.
    vim.api.nvim_buf_set_lines(0,target-1,target,false,{before..' '})
    vim.api.nvim_win_set_cursor(0,{target,0})
    vim.cmd('diffupdate')
    vim.cmd('normal! zv')
    vim.cmd('redraw!')
    local highlighted=vim.fn.diff_hlID(target,1)~=0
    emit({phase='diff_probe',status=highlighted and 'PASS'or'ERROR',diff_render_ms=(vim.uv.hrtime()-started)/1e6,hunk_count=highlighted and 1 or 0,diff=vim.wo.diff,modified=vim.bo.modified,saved=false})
   end)
   if not ok then emit({phase='diff_probe',status='ERROR'})end
   vim.defer_fn(cancel,200)
  end,500)
 end)
end})\n''');script.chmod(0o600)
        env=target_environment(guard.url,read_only=True)
        probe=Path(__file__).with_name('nvim_probe.py');env.update(TERM='xterm-256color',COLORTERM='truecolor',EDITOR=shlex.join([sys.executable,str(probe)]),DBCLI_BULK_EDITOR=shlex.join([sys.executable,str(probe)]),DBCLI_QUOTE_EVENTS=str(log),DBCLI_QUOTE_NVIM_SCRIPT=str(script),DBCLI_QUOTE_NVIM=args.nvim)
        try:
            started=time.monotonic_ns();terminal=Terminal(command(candidate,kind,args),folder,env);sampler=Sampler(terminal.process.pid)
            ready=InitialFrame(terminal,kind,args.table)
            if not terminal.wait(ready,45):
                result['initial_display']=ready.diagnostics()
                raise TimeoutError('initial_grid_not_ready')
            result['initial_display']=ready.diagnostics()
            result['initial_grid_ms']=(time.monotonic_ns()-started)/1e6;terminal.drain(.15);before=sampler.snapshot();pressed=time.monotonic_ns();terminal.send('E',.05)
            if not terminal.wait(lambda:any(v.get('phase')=='nvim_ready'for v in events(log)),90):raise TimeoutError('nvim_not_ready')
            ready_event=next(v for v in events(log)if v['phase']=='nvim_ready');launch=next(v for v in events(log)if v['phase']=='editor_launch');after=sampler.snapshot()
            result.update(e_to_editor_launch_ms=(launch['time_ns']-pressed)/1e6,e_to_nvim_ready_ms=(int(ready_event['time_ns'])-pressed)/1e6,nvim_load_ms=(int(ready_event['time_ns'])-launch['time_ns'])/1e6,file_bytes=launch['file_bytes'],file_count=launch['file_count'],nvim_lines=ready_event['line_count'],nvim_diff=ready_event['diff'],tree_rss_at_editor_ready_kib=after['rss_kib'],tree_sampled_peak_to_ready_kib=after['sampled_peak_rss_kib'],tree_observed_cpu_to_ready_ms=after['cpu_observed_seconds']*1000,tree_observed_e_cpu_ms=max(0,(after['cpu_observed_seconds']-before['cpu_observed_seconds'])*1000),memory_sample_lag_ms=(time.monotonic_ns()-int(ready_event['time_ns']))/1e6,nvim_present_at_ready=after['nvim_present'])
            result['readiness_sample_status']='ON_TIME' if 0<=result['memory_sample_lag_ms']<=100 and after['nvim_present'] else 'LATE_OR_MISSING'
            if not terminal.wait(lambda:any(v.get('phase')=='editor_exit'for v in events(log)),90):raise TimeoutError('nvim_did_not_cancel')
            exit_event=next(v for v in events(log)if v['phase']=='editor_exit');result.update(nvim_cpu_ms=exit_event['nvim_cpu_ms'],nvim_peak_rss_kib=exit_event['nvim_peak_rss_kib'],editor_exit_code=exit_event['exit_code']);terminal.drain(.2);result['timing_status']='PASS'
            diff_event=next((v for v in events(log)if v.get('phase')=='diff_probe'),{'status':'MISSING'})
            result['neovim_diff_probe']={k:v for k,v in diff_event.items()if k!='phase'}
            if exit_event['exit_code']!=23:raise RuntimeError('editor_did_not_exit_via_cquit23')
            if diff_event['status']!='PASS':raise RuntimeError('updated_diff_render_probe_failed')
            if launch['file_count']!=2 or not ready_event['diff']:raise RuntimeError('updated_editor_is_not_two_file_diff')
        except Exception as e:result.update(timing_status='ERROR',error_class=type(e).__name__,reason='benchmark_step_failed')
        finally:
            if terminal:result['lifecycle']=graceful_close(terminal,kind)
            result['write_guard']=guard.stats()
            if sampler:result['tree_lifetime_sampled']=sampler.close()
            result['cleanup_status']='PASS'if lifecycle_ok(result.get('lifecycle',{}))else 'ERROR'
            result['status']='PASS'if result['timing_status']=='PASS'and result['cleanup_status']=='PASS'else 'CLEANUP_ERROR'if result['timing_status']=='PASS'else 'ERROR'
            if guard.blocked:result['status']='BLOCKED_WRITE_ATTEMPT'
    return result


def main(argv=None):
    p = parser(__doc__,ui=True);args = p.parse_args(argv)
    try:
        selected = candidates(args,ui=True)
        args.nvim = shutil.which(args.nvim)
        if not args.nvim: raise ValueError('Neovim executable unavailable')
        if 'modern' in args.interfaces and not args.frontend.is_file(): raise ValueError('Build the frontend first')
        url,safe = target(args);meta = metadata(url,args.table)
        if meta['exact_rows'] == 0: raise ValueError('Neovim timing requires a nonempty table')
        fingerprints = provenance(selected,args.frontend if 'modern' in args.interfaces else None)+[dict(artifact(args.nvim),executable=True)]
        env = target_environment(url,read_only=True);env['DBCLI_PERF_TABLE'] = args.table
        env['DBCLI_QUOTES_JSON_COLUMNS'] = json.dumps([c['name'] for c in meta['columns'] if c['oid'] in (114,3802)])
        jobs = workloads(args.table,meta)
        expected = {label:digest_result(oracle(url,args.table,op),meta['columns']) for label,op in jobs.items()}
        report = {'status':'RUNNING','environment':environment(),'nvim_version':subprocess.check_output([args.nvim,'--version'],text=True,timeout=3).splitlines()[0],'target':safe,'table':meta,'seed':args.seed,'repetitions':args.repetitions,'artifacts':fingerprints,'validation':{},'results':[],'notes':[
            'All native candidates must match the PostgreSQL oracle before UI timing. No mutation is sent by the benchmark.',
            'UI sessions use the same NoTLS packet guard. Neovim changes unsaved JSON whitespace to verify a diff, then always exits :cq 23; confirmation is never sent.',
            'Initial frame includes 150ms quiescence; editor readiness is emitted from real Neovim VimEnter. Clean Neovim configuration is used.',
            'All OS threads are searched for descendants. UI, worker and Neovim RSS include shared mappings more than once; wrappers are excluded.',
            'Tree CPU is the sum of the last observed per-process counters. A 20ms sampler can miss short-lived CPU/peaks; memory readiness lag is reported per run.',
            'Warm database/OS caches; no cold-server claims, cache flushes or restarts. No screenshots or row values are retained.',
        ]}
        for name,candidate in selected.items():
            runner = Runner(candidate,env)
            try:
                checks = {}
                for label,operation in jobs.items():
                    got = runner.request(dict(operation,table=args.table))
                    checks[label] = bool(got.get('ok') and digest_result(got['value'],meta['columns'])==expected[label])
                report['validation'][name] = checks
            finally: runner.close()
        report['validation_passed'] = all(all(checks.values()) for checks in report['validation'].values())
        if report['validation_passed']:
            for repeat in range(args.repetitions):
                schedule = [(name,kind) for name in selected for kind in args.interfaces]
                random.Random(args.seed+repeat).shuffle(schedule)
                for name,kind in schedule:
                    record = one(selected[name],kind,url,args);record['repeat'] = repeat
                    report['results'].append(record);save(args.output,report)
                    print(name,kind,record['status'],round(record.get('e_to_nvim_ready_ms',0),1),flush=True)
        report['table_after'] = metadata(url,args.table)
        report['oracle_stable_after'] = all(digest_result(oracle(url,args.table,op),meta['columns'])==expected[label] for label,op in jobs.items())
        report['artifacts_unchanged'] = provenance(selected,args.frontend if 'modern' in args.interfaces else None)+[dict(artifact(args.nvim),executable=True)]==fingerprints
        report['status'] = 'PASS' if report['validation_passed'] and report['oracle_stable_after'] and report['artifacts_unchanged'] and all(r['status']=='PASS' for r in report['results']) else 'FAIL'
        save(args.output,report);print('report',args.output,report['status'])
        return int(report['status']!='PASS')
    except Exception as exc:
        if 'report' in locals():
            report.update(status='ERROR',error_class=type(exc).__name__)
            try: save(args.output,report)
            except OSError: pass
        print('UI benchmark failed:',type(exc).__name__,'(connection and row details suppressed)',file=sys.stderr)
        return 1
if __name__ == '__main__': raise SystemExit(main())
