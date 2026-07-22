#!/usr/bin/env python3
"""Deterministic 100-date Demo durability test with RSS checkpoints."""
import json, os, multiprocessing, psutil, time, sys, faulthandler, traceback, queue, threading
from datetime import date,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
def main():
 stamp=time.strftime('%Y%m%d_%H%M%S'); d=ROOT/'data'/'diagnostics'/('durability100_'+stamp); os.environ.update(INSTANCE_ID='durability100',INSTANCE_DATA_DIR=str(d),INSTANCE_PORT='8597',DB_BACKEND='sqlite'); d.mkdir(parents=True)
 faulthandler.enable(open(d/'faulthandler.log','w'))
 import app
 from services.job_runner import CancellationSignal
 ctx=multiprocessing.get_context('spawn'); q=ctx.Queue(); jid='durability100'; sig=CancellationSignal(ctx.Event(),app.CANCEL_FILE,jid); sig.reset()
 cfg=app.CollectionConfig(source='Demo',city_or_region='Durability',checkin_start=date(2026,1,1),checkin_end=date(2026,4,10),nights=1,adults=2,currency='USD',max_hotels=2,headless=True,collect_all_available=False,max_scroll_minutes=1,selected_star_ratings=(1,2,3,4,5),include_unknown_star_rating=True,debug_mode=False,screenshots_enabled=False,fast_mode=False,performance_mode='balanced',block_images_and_fonts=False,test_mode=True,db_backend='sqlite',hotels_only=True,disable_filters_during_complete_collection=False,ultra_reliable_loading_mode=False,resume_previous_run=False,auto_export_partial_excel=False,partial_export_frequency='every_5_dates')
 q=ctx.Queue(); p=ctx.Process(target=app.run_background_job_with_fatal_guard,args=(app.run_resilient_collection_job,cfg,sig,q,jid,str(d/'logs'/'run.log'))); p.start(); samples={}; started=time.time(); dates={}
 def drain():
  while p.is_alive():
   try: q.get(timeout=.2)
   except queue.Empty: pass
 threading.Thread(target=drain,daemon=True).start()
 while p.is_alive():
  elapsed=time.time()-started
  if elapsed>180: p.terminate(); break
  try: rss=psutil.Process(p.pid).memory_info().rss//1048576
  except psutil.Error: rss=-1
  samples.setdefault(str(int(elapsed)),rss)
  if int(elapsed)%5==0: print(f'progress elapsed={int(elapsed)} child={p.pid} rss_mb={rss}',flush=True)
  time.sleep(1)
 p.join(timeout=5); report={'exit_code':p.exitcode,'samples_mb':samples,'data_dir':str(d),'traceback':None}
 if p.is_alive(): p.terminate(); p.join(); report['timeout']=True
 try:
  import sqlite3
  c=sqlite3.connect(d/'hotel_price_collector.sqlite'); report['sqlite_rows']=c.execute('select count(*) from hotel_price_results').fetchone()[0]; report['dates']=c.execute('select count(distinct checkin_date) from hotel_price_results').fetchone()[0]
 except Exception: report['traceback']=traceback.format_exc()
 (d/'durability_report.json').write_text(json.dumps(report,indent=2)); print(json.dumps(report,indent=2)); return 0 if p.exitcode==0 and not report.get('timeout') else 1
if __name__=='__main__': raise SystemExit(main())
