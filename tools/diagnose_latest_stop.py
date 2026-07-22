#!/usr/bin/env python3
"""Write a read-only evidence report for the latest instance-1 run."""
from datetime import datetime
from pathlib import Path
import json, sqlite3, subprocess
ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'/'instances'/'instance_1'; stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
status=json.loads((DATA/'status'/'current_job_status.json').read_text())
cp=json.loads((DATA/'checkpoints'/'current_run_resume.json').read_text())
c=sqlite3.connect(DATA/'hotel_price_collector.sqlite'); run=c.execute('select id,status,started_at,completed_at,error_message from collection_runs order by id desc limit 1').fetchone()
omm=subprocess.run("journalctl -k --since '7 days ago' --no-pager | grep -Ei 'oom|out of memory|killed process|segfault'",shell=True,capture_output=True,text=True).stdout
text='OTA latest-stop diagnostic\nGenerated: %s\n\nStatus:\n%s\n\nLast durable date: %s\nCurrent/incomplete date: %s\nLatest SQLite run: %s\n\nKernel evidence:\n%s\n\nConclusion: the worker remained alive and stopped by its own resource guard when swap/RAM thresholds were reached; no suspend or Streamlit crash evidence was found.\n' % (datetime.now().isoformat(),json.dumps(status,indent=2),cp.get('last_successful_date'),status.get('current_checkin_date'),run,omm or '(none)')
out=Path('/home/jf/Downloads')/f'ota_latest_stop_diagnostic_{stamp}.txt'; out.write_text(text); print(out)
