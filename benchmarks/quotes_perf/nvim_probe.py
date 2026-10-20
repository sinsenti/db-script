#!/usr/bin/env python3
"""Real clean-profile Neovim probe: sizes/times only; never saves or confirms."""
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time


def emit(value):
    with open(os.environ['DBCLI_QUOTE_EVENTS'],'a') as out:
        out.write(json.dumps(value)+'\n')


def main():
    files=[Path(arg) for arg in sys.argv[1:] if arg.endswith('.json') and Path(arg).is_file()]
    emit({'phase':'editor_launch','time_ns':time.monotonic_ns(),'file_count':len(files),'file_bytes':[path.stat().st_size for path in files]})
    script=Path(os.environ['DBCLI_QUOTE_NVIM_SCRIPT'])
    args=['-u','NONE','-n','-i','NONE','--cmd','set noundofile noswapfile nobackup nowritebackup','--cmd','lua dofile('+json.dumps(str(script))+')',*sys.argv[1:]]
    before=resource.getrusage(resource.RUSAGE_CHILDREN)
    process=subprocess.Popen([os.environ['DBCLI_QUOTE_NVIM'],*args])
    def stop(signum,frame):
        if process.poll() is None: process.send_signal(signum)
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired: process.kill();process.wait()
        raise SystemExit(128+signum)
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,stop)
    try: code=process.wait()
    finally:
        if process.poll() is None: process.kill();process.wait()
    after=resource.getrusage(resource.RUSAGE_CHILDREN)
    emit({'phase':'editor_exit','time_ns':time.monotonic_ns(),'exit_code':code,'nvim_cpu_ms':((after.ru_utime-before.ru_utime)+(after.ru_stime-before.ru_stime))*1000,'nvim_peak_rss_kib':after.ru_maxrss})
    return code
if __name__=='__main__':raise SystemExit(main())
