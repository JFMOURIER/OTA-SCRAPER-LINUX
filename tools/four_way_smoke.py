#!/usr/bin/env python3
"""Tiny Demo-only isolation test; never touches production databases."""
from __future__ import annotations
import json, subprocess, sys, time
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S'); base=ROOT/'data'/'diagnostics'/('four_way_'+stamp); base.mkdir(parents=True)
    procs=[]
    for i in range(1,5):
        d=base/f'instance_{i}'; d.mkdir()
        procs.append(subprocess.Popen([str(ROOT/'.venv/bin/python'),str(ROOT/'tools/worker_smoke.py'),'--max-hotels','2','--data-dir',str(d)],cwd=ROOT,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT))
        if i<4: time.sleep(2)
    results=[]
    for i,p in enumerate(procs,1):
        out,_=p.communicate(timeout=45); (base/f'instance_{i}.log').write_text(out)
        results.append({'instance':i,'exit_code':p.returncode,'database':str(base/f'instance_{i}'/'hotel_price_collector.sqlite')})
    payload={'mode':'Demo low-memory controlled test','base':str(base),'instances':results,'unique_databases':len({x['database'] for x in results})==4}
    (base/'report.json').write_text(json.dumps(payload,indent=2)); print(json.dumps(payload,indent=2)); return 0 if all(x['exit_code']==0 for x in results) else 1
if __name__=='__main__': raise SystemExit(main())
