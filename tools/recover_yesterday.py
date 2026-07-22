#!/usr/bin/env python3
"""Transactionally inspect and export the most recent run for a calendar day."""
from __future__ import annotations
import argparse, csv, hashlib, json, os, sqlite3, tempfile
from datetime import datetime, timedelta
from pathlib import Path

def atomic_csv(path: Path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    n=0
    try:
        with os.fdopen(fd,'w',encoding='utf-8-sig',newline='') as f:
            w=csv.writer(f, quoting=csv.QUOTE_MINIMAL); w.writerow(header)
            for row in rows: w.writerow(row); n+=1
            f.flush(); os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)
    return n

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--date',required=True); ap.add_argument('--db',required=True); ap.add_argument('--out-dir',required=True); args=ap.parse_args()
    db=Path(args.db).resolve(); out=Path(args.out_dir).resolve(); stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    c=sqlite3.connect(str(db)); c.row_factory=sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    runs=[dict(r) for r in c.execute("select * from collection_runs where date(started_at)=? or date(completed_at)=? order by id",(args.date,args.date))]
    cols=[r[1] for r in c.execute('pragma table_info(hotel_price_results)')]
    header=['instance_id','run_id','requested_date','extraction_timestamp','source_database']+cols
    q='select '+','.join('h.'+x for x in cols)+', r.checkin_date requested_date from hotel_price_results h left join collection_runs r on r.id=h.collection_run_id where date(h.collected_at)=? or date(r.started_at)=? order by h.id'
    rows=(("instance_1",r['collection_run_id'],r['requested_date'],r['collected_at'],str(db),*[r[x] for x in cols]) for r in c.execute(q,(args.date,args.date)))
    csv_path=Path('/home/jf/Downloads')/f'OTA_RECOVERED_FULL_{stamp}.csv'; n=atomic_csv(csv_path,header,rows)
    sha=hashlib.sha256(csv_path.read_bytes()).hexdigest()
    dates=list(c.execute("select checkin_date,count(*),count(distinct hotel_name),min(collected_at),max(collected_at) from hotel_price_results where date(collected_at)=? group by checkin_date order by checkin_date",(args.date,)))
    summary=out/f'recovery_date_summary_{stamp}.csv'; atomic_csv(summary,['requested_date','row_count','distinct_hotels','first_insert','last_insert'],dates)
    missing=out/f'missing_or_incomplete_dates_{stamp}.csv'; atomic_csv(missing,['requested_date','status','row_count','run_ids','notes'],((r['checkin_date'],r['status'],c.execute('select count(*) from hotel_price_results where collection_run_id=?',(r['id'],)).fetchone()[0],r['id'],r.get('error_message') or '') for r in runs))
    manifest={'csv_path':str(csv_path),'csv_bytes':csv_path.stat().st_size,'sha256':sha,'exported_rows':n,'distinct_dates':len({x[0] for x in dates}),'distinct_hotels':sum(1 for _ in c.execute("select distinct hotel_name from hotel_price_results where date(collected_at)=?",(args.date,))),'source_databases':[str(db)],'source_database_rows':c.execute('select count(*) from hotel_price_results').fetchone()[0],'runs':runs,'summary':str(summary),'missing_or_incomplete':str(missing),'export_timestamp':datetime.now().isoformat()}
    mp=Path('/home/jf/Downloads')/f'OTA_RECOVERED_FULL_{stamp}_manifest.json'; mp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    report=out/f'recovery_report_{stamp}.txt'; report.write_text('Recovery report for '+args.date+'\nDatabase: '+str(db)+'\nRows exported: '+str(n)+'\nRuns:\n'+json.dumps(runs,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(manifest,ensure_ascii=False))
if __name__=='__main__': main()
