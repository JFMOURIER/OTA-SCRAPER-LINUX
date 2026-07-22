#!/usr/bin/env python3
"""Persistent per-instance worker supervisor.

The UI submits an atomic JSON request; this process owns the scraper child and
survives UI restarts.  It deliberately never scans or kills unrelated chrome.
"""
from __future__ import annotations
import json, os, signal, subprocess, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    iid=os.getenv('INSTANCE_ID','instance_1'); data=Path(os.getenv('INSTANCE_DATA_DIR',ROOT/'data/instances'/iid)); status=data/'status'; status.mkdir(parents=True,exist_ok=True)
    req=status/'worker_request.json'; pidfile=status/'worker_supervisor.pid'; pidfile.write_text(str(os.getpid()))
    restarts=[]; child=None; stopping=False
    def stop(_s,_f):
        nonlocal stopping
        stopping=True
        if child and child.poll() is None: child.terminate()
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    try:
        while not stopping:
            if child is None or child.poll() is not None:
                if child is not None and child.returncode not in (0,):
                    now=time.time(); restarts[:]=[x for x in restarts if now-x<3600]
                    if len(restarts)>=5: (status/'worker_supervisor_error.json').write_text(json.dumps({'reason':'restart_budget_exhausted','exit_code':child.returncode})); time.sleep(10); continue
                    restarts.append(now)
                if req.exists():
                    try: payload=json.loads(req.read_text()); req.unlink()
                    except Exception: time.sleep(1); continue
                    log=data/'logs'/'worker_supervisor.log'; log.parent.mkdir(parents=True,exist_ok=True)
                    cmd=[str(ROOT/'.venv/bin/python'),str(ROOT/'tools/ota_worker_child.py'),json.dumps(payload)]
                    child=subprocess.Popen(cmd,cwd=ROOT,start_new_session=True,stdout=log.open('a'),stderr=subprocess.STDOUT,env=os.environ.copy())
                    (status/'worker_child.pid').write_text(str(child.pid))
            time.sleep(2)
    finally:
        if child and child.poll() is None: child.terminate(); child.wait(timeout=20)
        for p in (pidfile,status/'worker_child.pid'):
            try: p.unlink()
            except FileNotFoundError: pass
    return 0
if __name__=='__main__': raise SystemExit(main())
