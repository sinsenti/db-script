#!/usr/bin/env python3
"""Print concise Markdown tables from the standalone benchmark JSON reports."""
import argparse
import json
import statistics
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports',nargs='+',type=Path)
    args=parser.parse_args()
    for path in args.reports:
        report=json.loads(path.read_text())
        print('\n## '+path.name+' — '+report['status']+'\n')
        if 'candidates' in report:
            print('| Candidate | Workload | Median ms | CPU ms/call | Worker peak MiB |\n|---|---|---:|---:|---:|')
            for name,candidate in report['candidates'].items():
                print('| '+name+' | fresh process + SELECT 1 | '+str(round(candidate.get('fresh_process_query_median_ms',0),2))+' | — | — |')
                for label,work in candidate['workloads'].items():
                    if not work.get('samples_ms'): print('| '+name+' | '+label+' | '+work['status']+' | — | — |');continue
                    print(f"| {name} | {label} | {work['median_ms']:.2f} | {work['worker_cpu_ms_per_call']:.2f} | {work['worker_peak_rss_kib']/1024:.1f} |")
        else:
            print('| Candidate | Interface | Samples | Startup ms | E → Neovim ms | Observed CPU to ready ms | RSS at ready MiB |\n|---|---|---:|---:|---:|---:|---:|')
            groups={}
            for record in report['results']:groups.setdefault((record['implementation'],record['interface']),[]).append(record)
            for (name,kind),records in groups.items():
                good=[r for r in records if r['status']=='PASS']
                def median(key,scale=1):
                    records = good if key in {'initial_grid_ms','e_to_nvim_ready_ms'} else [r for r in good if r.get('readiness_sample_status')=='ON_TIME']
                    return f"{statistics.median(r[key] for r in records)/scale:.1f}" if records else '—'
                print(f"| {name} | {kind} | {len(good)}/{len(records)} | {median('initial_grid_ms')} | {median('e_to_nvim_ready_ms')} | {median('tree_observed_cpu_to_ready_ms')} | {median('tree_rss_at_editor_ready_kib',1024)} |")
        print('\nArtifact sizes (individual files; excludes shared libraries):\n')
        for item in report['artifacts']:
            if item.get('executable', '/target/' in item['path'] or (Path(item['path']).name.startswith('python') and not item['path'].endswith('.py'))):
                print(f"- `{item['path']}`: {item['bytes']/1048576:.2f} MiB; SHA-256 `{item['sha256']}`")
    return 0
if __name__=='__main__':raise SystemExit(main())
