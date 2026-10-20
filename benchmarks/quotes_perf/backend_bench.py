#!/usr/bin/env python3
"""Validate then time local PostgreSQL reads with main's Rust and Python backends."""
from __future__ import annotations
import json
import random
import statistics
import time
from common import target, metadata, connect, encode, json_wire, digest_result, proc_usage, target_environment, exact_dumps, table_ref, table_parts
from config import parser, candidates, provenance, save, environment
from client import Runner


def workloads(table, meta, predicate=None):
    if len(meta['primary_key']) != 1: raise ValueError('Page comparisons require a single-column primary key')
    key = meta['primary_key'][0]
    quote = lambda value: '"'+value.replace('"','""')+'"'
    predicate = predicate or quote(key)+' IS NOT NULL'
    return {
        'tables':{'op':'tables'},
        'first_page':{'op':'page','limit':25,'offset':0,'order_by':[key,'ASC']},
        'next_page':{'op':'page','limit':25,'offset':25,'order_by':[key,'ASC']},
        'descending_page':{'op':'page','limit':25,'offset':0,'order_by':[key,'DESC']},
        'filtered_page':{'op':'page','limit':25,'offset':0,'order_by':[key,'ASC'],'filter':predicate},
        'count':{'op':'count'},'schema':{'op':'schema'},
        'same_sql_page':{'op':'raw','sql':'SELECT * FROM '+table_ref(table)+' ORDER BY '+quote(key),'limit':25,'offset':0},
        'bulk_fetch':{'op':'page','limit':100000,'offset':0,'order_by':[key,'ASC']},
    }


def oracle(url, table, op):
    kind = op['op'];reference = table_ref(table);schema,name = table_parts(table)
    with connect(url) as con:
        if kind == 'tables':
            return [schema+'.'+row[0] for row in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name",(schema,)).fetchall()]
        if kind == 'count': return con.execute('SELECT count(*) FROM '+reference).fetchone()[0]
        if kind == 'schema':
            rows = con.execute("SELECT column_name,data_type,is_nullable,column_default FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",(schema,name)).fetchall()
            return {'headers':['Column Name','Type','Nullable','Default'],'rows':[list(row) for row in rows]}
        if kind == 'raw':
            sql = op['sql']+' LIMIT %s OFFSET %s';args = (op['limit'],op.get('offset',0))
        else:
            key,direction = op['order_by']
            quoted = '"'+key.replace('"','""')+'"'
            sql = 'SELECT * FROM '+reference+(' WHERE '+op['filter'] if op.get('filter') else '')+' ORDER BY '+quoted+' '+direction+' LIMIT %s OFFSET %s'
            args = (op['limit'],op['offset'])
        cur = con.execute(sql,args)
        return {'headers':[d.name for d in cur.description], 'rows':[[json_wire(v) if d.type_code in (114,3802) else encode(v) for d,v in zip(cur.description,row)] for row in cur.fetchall()]}


def main(argv=None):
    p = parser(__doc__);args = p.parse_args(argv)
    try:
        selected = candidates(args);url,safe = target(args);meta = metadata(url,args.table)
        jobs = workloads(args.table,meta,args.filter)
        env = target_environment(url,read_only=True)
        env['DBCLI_PERF_TABLE'] = args.table
        env['DBCLI_QUOTES_JSON_COLUMNS'] = json.dumps([c['name'] for c in meta['columns'] if c['oid'] in (114,3802)])
        fingerprints = provenance(selected)
        report = {'status':'RUNNING','environment':environment(),'target':safe,'table':meta,'repetitions':args.repetitions,'seed':args.seed,'artifacts':fingerprints,'candidates':{},'notes':[
            'Reads only. Every connection uses bounded server read-only transactions; no cache flushes or server restarts.',
            'Warm timings reuse processes; connections remain short-lived per operation. Fresh-process startup includes SELECT 1 against a warm server, not a cold database.',
            'Every candidate must match every oracle workload before any timings are collected. Timed responses are validated too.',
            'Native table discovery spans schemas and is filtered to the target schema for comparison with Python. Use same_sql_page for identical supplied SQL.',
            'Worker CPU/RSS exclude database server and harness. Linux /proc CPU granularity applies; VmHWM covers the entire worker lifetime.',
            'No rows, credentials, SQL predicates or screens are written to reports; only aggregates and result digests.',
        ]}
        expected = {label:oracle(url,args.table,op) for label,op in jobs.items()}
        digests = {label:digest_result(value,meta['columns']) for label,value in expected.items()}
        rng = random.Random(args.seed);runners = {}
        try:
            order = list(selected);rng.shuffle(order)
            for name in order:
                record = {'status':'PASS','workloads':{}};report['candidates'][name] = record
                try:
                    runner = Runner(selected[name],env);runners[name] = runner
                    for label,operation in jobs.items():
                        got = runner.request(dict(operation,table=args.table))
                        valid = got.get('ok') and digest_result(got['value'],meta['columns']) == digests[label]
                        record['workloads'][label] = {'status':'PASS' if valid else 'MISMATCH','samples_ms':[],'cpu_samples_ms':[]}
                        if not valid: record['status'] = 'VALIDATION_FAILED'
                        else:
                            record['workloads'][label].update(result_sha256=digests[label], reencoded_result_json_bytes=len(exact_dumps(got['value']).encode()))
                except Exception as exc: record.update(status='ERROR',error_class=type(exc).__name__)
                print('validated',name,record['status'],flush=True)
            report['validation_passed'] = all(r['status']=='PASS' for r in report['candidates'].values())
            if report['validation_passed']:
                for repeat in range(args.repetitions):
                    schedule = [(name,label) for name in selected for label in jobs];rng.shuffle(schedule)
                    for name,label in schedule:
                        runner = runners[name];work = report['candidates'][name]['workloads'][label]
                        before = proc_usage(runner.proc.pid);started = time.perf_counter()
                        got = runner.request(dict(jobs[label],table=args.table));duration = (time.perf_counter()-started)*1000
                        after = proc_usage(runner.proc.pid)
                        if not got.get('ok') or digest_result(got['value'],meta['columns']) != digests[label]:
                            work['status'] = 'TIMED_RESULT_CHANGED';report['candidates'][name]['status'] = 'RESULT_CHANGED'
                            continue
                        work['samples_ms'].append(duration);work['cpu_samples_ms'].append(max(0,(after['cpu_seconds']-before['cpu_seconds'])*1000))
                        work.update(worker_rss_kib=after['rss_kib'],worker_peak_rss_kib=after['peak_rss_kib'])
                    for name in selected:
                        fresh = None
                        try:
                            started = time.perf_counter();fresh = Runner(selected[name],env)
                            got = fresh.request({'op':'raw','sql':'SELECT 1 AS probe','limit':1,'offset':0})
                            elapsed = (time.perf_counter()-started)*1000
                            if got != {'ok':True,'value':{'headers':['probe'],'rows':[[1]]}}: raise RuntimeError('startup_probe_mismatch')
                            report['candidates'][name].setdefault('fresh_process_query_samples_ms',[]).append(elapsed)
                        finally:
                            if fresh: fresh.close()
                    save(args.output,report);print('round',repeat+1,flush=True)
            for record in report['candidates'].values():
                for work in record['workloads'].values():
                    if work['samples_ms']: work.update(median_ms=statistics.median(work['samples_ms']),min_ms=min(work['samples_ms']),max_ms=max(work['samples_ms']),worker_cpu_ms_per_call=statistics.mean(work['cpu_samples_ms']))
                if record.get('fresh_process_query_samples_ms'): record['fresh_process_query_median_ms'] = statistics.median(record['fresh_process_query_samples_ms'])
            report['table_after'] = metadata(url,args.table)
            report['oracle_stable_after'] = all(digest_result(oracle(url,args.table,op),meta['columns'])==digests[label] for label,op in jobs.items())
            report['artifacts_unchanged'] = provenance(selected)==fingerprints
            report['status'] = 'PASS' if report['validation_passed'] and report['oracle_stable_after'] and report['artifacts_unchanged'] and all(r['status']=='PASS' for r in report['candidates'].values()) else 'FAIL'
            save(args.output,report)
        finally:
            for runner in runners.values(): runner.close()
        print('report',args.output,report['status'])
        return int(report['status']!='PASS')
    except Exception as exc:
        if 'report' in locals():
            report.update(status='ERROR',error_class=type(exc).__name__)
            try: save(args.output,report)
            except OSError: pass
        print('Benchmark failed:',type(exc).__name__,'(connection and row details suppressed)',file=__import__('sys').stderr)
        return 1
if __name__ == '__main__': raise SystemExit(main())
