#!/usr/bin/env python3
import json,sys
from datetime import date
from pathlib import Path
import os
payload=json.loads(sys.argv[1]); os.environ.update({str(k):str(v) for k,v in payload.get('env',{}).items()})
import multiprocessing
import app
from services.job_runner import CancellationSignal
cfg=app.CollectionConfig(source=payload.get('source','Demo'),city_or_region=payload.get('city','Test City'),checkin_start=date.fromisoformat(payload['start_date']),checkin_end=date.fromisoformat(payload['end_date']),nights=1,adults=2,currency='USD',max_hotels=int(payload.get('max_hotels',2)),headless=True,collect_all_available=False,max_scroll_minutes=1,selected_star_ratings=(1,2,3,4,5),include_unknown_star_rating=True,debug_mode=False,screenshots_enabled=False,fast_mode=False,performance_mode='balanced',block_images_and_fonts=False,test_mode=True,db_backend='sqlite',hotels_only=True,disable_filters_during_complete_collection=False,ultra_reliable_loading_mode=False,resume_previous_run=True,auto_export_partial_excel=False,partial_export_frequency='every_5_dates')
ctx=multiprocessing.get_context('spawn'); q=ctx.Queue(); jid=payload.get('job_id','external'); sig=CancellationSignal(ctx.Event(),app.CANCEL_FILE,jid); sig.reset(); app.run_resilient_collection_job(cfg,sig,q)
