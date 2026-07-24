# OTA-SCRAPER-LINUX

## Safe Linux tower operation

Normal starts never install or upgrade packages. Run setup separately, once:

```bash
cd /home/jf/Projects/OTA-SCRAPER-LINUX
scripts/setup-linux
scripts/ota-ui install-service
```

Operate the Streamlit interface with:

```bash
scripts/ota-ui start
scripts/ota-ui stop
scripts/ota-ui restart
scripts/ota-ui status
scripts/ota-ui logs
```

The main instance binds explicitly to `0.0.0.0:8501` and uses
`data/instances/instance_1`. Dependency and browser updates are an explicit
maintenance action via `scripts/update-dependencies`.

Each instance is limited to one managed worker/browser. A shared host semaphore
defaults to one active instance, so the current 8 GB machine still runs only one
scraper at a time. SQLite is the live data store; successful-run screenshots are
off by default; partial Excel snapshots are off by default and, when enabled,
default to every 25 completed dates. Resource thresholds can be adjusted with
`OTA_MIN_START_AVAILABLE_MB`, `OTA_EMERGENCY_AVAILABLE_MB`,
`OTA_MIN_SWAP_FREE_MB`, `OTA_WARN_BROWSER_RSS_MB`, and
`OTA_STOP_BROWSER_RSS_MB`.

Run a controlled one-date diagnostic (maximum 10 properties) with:

```bash
scripts/run-scraper-diagnostic --destination Orlando --date 2026-08-01 --max-properties 3 --mode both
```

Read and export a SQLite database without modifying it with:

```bash
scripts/recover-data data/instances/instance_1/hotel_price_collector.sqlite
```

## A. Project description

OTA-SCRAPER-LINUX is the Ubuntu/Linux version of the OTA hotel price scraper. It keeps the Streamlit dashboard, scraping workflow, local SQLite storage, Excel exports, checkpoints, screenshots, and visible logs from the working OTA Scraper while using Linux-compatible paths and Playwright Chromium setup.

The app can be launched manually with:

```bash
.venv/bin/streamlit run app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true
```

Runtime files are created locally under `data/`, including exports, SQLite databases, screenshots, debug files, checkpoints, browser profiles, partial scrape files, and logs.

## B. Linux installation

Run these commands on your Linux tower PC:

```bash
cd ~
git clone https://github.com/JFMOURIER/OTA-SCRAPER-LINUX.git
cd OTA-SCRAPER-LINUX
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install --with-deps chromium
chmod +x run_linux.sh
./run_linux.sh
```

Then open:

```text
http://localhost:8501
```

## C. Clone from GitHub

```bash
cd ~
git clone https://github.com/JFMOURIER/OTA-SCRAPER-LINUX.git
cd OTA-SCRAPER-LINUX
```

## D. Create Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

Use the same virtual environment every time you run the app:

```bash
cd ~/OTA-SCRAPER-LINUX
source .venv/bin/activate
```

## E. Install Python requirements

```bash
pip install -r requirements.txt
```

Main Python packages include Streamlit, Playwright, pandas, Excel writers, psycopg, and python-dotenv.

## F. Install Playwright and Linux browser dependencies

Install Chromium and the Linux packages Playwright needs:

```bash
python -m playwright install --with-deps chromium
```

If Ubuntu reports missing packages, update apt and install the browser dependencies again:

```bash
sudo apt update
python -m playwright install --with-deps chromium
```

If your Ubuntu version does not provide one of the audio packages Playwright asks for, install the package name suggested by Ubuntu and rerun the Playwright command.

## G. Configure .env

Create your local `.env` from the safe template:

```bash
cp .env.example .env
nano .env
```

The default configuration uses SQLite:

```text
DB_BACKEND=sqlite
INSTANCE_DATA_DIR=data/instances/instance_1
```

Only add real API keys or database passwords to `.env`. Do not commit `.env`; it is ignored by git.

## H. Launch the application

Recommended launcher:

```bash
chmod +x run_linux.sh
./run_linux.sh
```

Manual launch (the `server.headless` setting affects Streamlit, not the scraper browser mode):

```bash
.venv/bin/streamlit run app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true
```

Use another port if `8501` is already busy:

```bash
streamlit run app.py --server.port 8502
```

## I. Scraper concurrency safety

Do not run the legacy `run_scraper_1.sh`, `run_scraper_2.sh`, or
`run_scraper_3.sh` launchers concurrently on this 8 GB tower. The supported UI
launcher is `scripts/ota-ui`. Every instance has its own non-overlap lock and all
instances share a configurable host semaphore. The semaphore defaults to one
worker. A value above one is rejected unless the 32 GB hardware preflight has
passed and concurrency has been explicitly enabled.

## J. Troubleshooting

`ModuleNotFoundError`: activate the virtual environment and reinstall requirements.

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

Chromium does not start or Playwright says the executable is missing:

```bash
python -m playwright install --with-deps chromium
```

`Permission denied: ./run_linux.sh`:

```bash
chmod +x run_linux.sh
```

The app starts and stops without obvious output: check the Streamlit log panel and local files under:

```text
data/instances/instance_1/logs/
data/instances/instance_1/status/
data/instances/instance_1/debug/
```

CAPTCHA or access restriction: the scraper is designed to stop safely, save partial results where possible, and mark the date as blocked. It does not bypass website security controls.

## K. GitHub update workflow

Use this workflow when updating the Linux project from the Linux tower PC:

```bash
cd ~/OTA-SCRAPER-LINUX
git pull
scripts/update-dependencies
scripts/ota-ui restart
```

To save your own code changes:

```bash
git status
git add app.py collectors services database tools requirements.txt README.md .gitignore .env.example run_linux.sh run_scraper_1.sh run_scraper_2.sh run_scraper_3.sh
git commit -m "Describe your Linux scraper update"
git push
```

Never commit `.env`, `data/`, SQLite databases, Excel exports, CSV files, screenshots, logs, debug files, checkpoints, or browser profiles.

## L. Automatic complete-run exports

When a SQLite run reaches a terminal status, its status transaction is committed
before the automatic CSV callback runs. The callback queries
`hotel_price_results` by the explicit `collection_run_id`, streams all rows in
bounded batches, writes UTF-8 with BOM to a same-directory temporary file, and
publishes the file without replacing an existing path. It does not read the
dashboard result list or its 500-row preview.

The instance copy is written below:

```text
data/instances/<INSTANCE_ID>/exports/
```

and is then copied atomically to `/home/jf/Downloads/`. Complete runs use
`_final_` in the timestamped filename. Terminal stopped or failed runs with
usable rows use `_partial_`. The `collection_runs` row records
`csv_export_status`, `csv_file_path`, `csv_downloads_path`,
`csv_rows_exported`, `csv_exported_at`, and `csv_export_error`. A successful
automatic export is reused when the completion callback is repeated. The
explicit Excel control in Streamlit selects a stored run ID and streams all of
that run's SQLite rows with its stored dates.

## M. Three configurable scheduled interfaces

The three independent control panels are:

| Interface | Instance ID | Local URL | Network URL |
| --- | --- | --- | --- |
| Near | `near_30_days` | `http://localhost:8501/` | `http://192.168.1.144:8501/` |
| Medium | `medium_31_120_days` | `http://localhost:8502/` | `http://192.168.1.144:8502/` |
| Long | `long_121_365_days` | `http://localhost:8503/` | `http://192.168.1.144:8503/` |

Each instance has its own SQLite database, configuration, exports, partials,
status/history, logs, debug files, checkpoints, browser profiles, heartbeat,
PID file, instance lock, and Drive upload state below
`data/instances/<INSTANCE_ID>/`. The historical
`data/instances/period_1/` directory is not part of these instances and is never
migrated or merged.

The shared Europe/Paris rolling-date resolver calculates all windows from one
anchor immediately before a run:

- 8501: `anchor` through `anchor + 1 calendar month - 1 day`.
- 8502: `anchor + 1 calendar month` through
  `anchor + 4 calendar months - 1 day`.
- 8503: `anchor + 4 calendar months` through the earlier of
  `anchor + 12 calendar months - 1 day` and `anchor + 365 days`.

Thus 8503 owns only the final approximately eight months of the single annual
horizon; it does not add twelve more months from its starting date. The
resolver verifies both boundaries are contiguous, none overlap, every horizon
date has one owner, and the last date never exceeds the 365-day hard cap.

Every Automatic Schedule panel previews the current automatic dates even while
disabled. Manual fixed dates can be saved instead and remain unchanged on
future runs. A manual range over 365 days requires explicit confirmation.
Selecting automatic dates again recalculates from the current Europe/Paris
date. Editing fields alone changes nothing: **Save Schedule** atomically
validates and persists the complete collection template. Saving does not run a
scraper.

Frequency controls are independent:

- 8501 accepts an integer from 15 through 1440 minutes and defaults to 60.
  Interval boundaries are based on scheduled start times, not page refresh or
  completion times.
- 8502 accepts 1–4 runs/day and defaults to 2 at `00:20` and `12:20`.
  Defaults for 1, 3, and 4 are `00:20`; `00:20, 08:20, 16:20`; and
  `00:20, 06:20, 12:20, 18:20`.
- 8503 accepts 1–2 runs/day and defaults to 1 at `01:35`; the two-run default is
  `01:35, 13:35`.

Daily times are editable but must be valid, unique, and sorted. All schedules
start disabled. Use **Save Schedule**, then enable explicitly. **Disable
Schedule** prevents future slots. **Run Once Now** creates one immediate slot
without changing the recurring calendar and obeys both locks. The UI displays
next/last run state, skip/defer reasons, worker and host capacity, recent
append-only history, local exports, and Drive state.

Persistent files are:

```text
data/instances/<INSTANCE_ID>/config/schedule.json
data/instances/<INSTANCE_ID>/status/schedule_state.json
data/instances/<INSTANCE_ID>/status/schedule_history.jsonl
data/instances/<INSTANCE_ID>/status/drive_uploads/run_<RUN_ID>.json
```

JSON configuration and state use same-directory temporary files, `fsync`, and
atomic replacement. Invalid input never replaces a valid schedule.

## N. Dispatcher, locking, and user services

Install and start the user services:

```bash
scripts/ota-scheduler prepare
scripts/ota-scheduler install-user
scripts/ota-scheduler enable-uis
scripts/ota-scheduler enable-dispatcher
```

The three UI units are `ota-ui-near.service`,
`ota-ui-medium.service`, and `ota-ui-long.service`. The scheduler uses
`ota-scheduler-dispatch.timer`, `ota-scheduler-dispatch.service`, and
`ota-scraper-run@.service`. Pending Drive-only retries use
`ota-drive-retry@.service`. The minute dispatcher is independent of Streamlit;
opening, refreshing, closing, or restarting a UI cannot launch or cancel a
scheduled run. Its timer uses `Persistent=false`, so powered-off time is not
backfilled.

Every daily slot key contains its local scheduled date/time; every interval
slot key contains its interval boundary. Dispatched keys persist, so a
dispatcher restart cannot launch a slot twice. Only one pending due slot per
instance is retained. If the same instance is already active, the event is
`scheduled_run_skipped_previous_run_active`. If another instance holds the
shared host slot, it is `scheduled_run_deferred_host_capacity` and is retried
on later ticks until its grace period expires. This capacity condition does not
mark the collection failed.

The old fixed timers (`ota-scraper-near.timer`,
`ota-scraper-medium.timer`, and `ota-scraper-long.timer`) must remain disabled.
The dispatcher refuses migration while any is active.

Inspect schedules, runs, and logs with:

```bash
scripts/ota-scheduler status
systemctl --user list-timers --all | grep ota
journalctl --user -u ota-scheduler-dispatch.service -n 100 --no-pager
journalctl --user -u 'ota-scraper-run@*.service' -n 100 --no-pager
journalctl --user -u ota-ui-near.service -n 100 --no-pager
```

Disable all schedules without deleting them, or stop all UIs, with:

```bash
scripts/ota-scheduler disable-all-schedules
scripts/ota-scheduler stop-uis
```

User services continue after desktop logout when lingering is enabled. Inspect
it with `loginctl show-user "$USER" -p Linger`. If it says `Linger=no`, an
administrator can run `sudo loginctl enable-linger jf`; the project scripts do
not run that command automatically.

## O. Local complete exports and Google Drive delivery

After finalized SQLite rows reach a terminal run state, complete CSV and Excel
files are queried by explicit run ID. They never use the dashboard's 500-row
preview. CSV is UTF-8 with BOM. Excel contains `All Hotel Results`,
`Daily Summary`, and `Run Summary`; CSV, Excel, and SQLite hotel-row counts
must match. Same-directory atomic publication never overwrites an existing
file.

Files are written below the instance `exports/` directory, then copied
atomically to `/home/jf/Downloads/`. Filenames contain source, city, instance,
run ID, resolved dates, `final` or `partial`, and a timestamp. Fully successful
runs use `final`; stopped or failed runs with usable finalized rows use
`partial`. Failed runs with zero rows do not create misleading empty exports.
Export failure never removes or downgrades collected SQLite data.

Drive upload starts only after both local artifacts succeed. The dedicated
folder IDs are:

| Instance | Google Drive folder ID |
| --- | --- |
| `near_30_days` | `18kkxujFodzEfoOmhfpBuZI8xDzaoUk8b` |
| `medium_31_120_days` | `1ATZztmP0pS9c0oprzF3LzQKYcpsXtT7S` |
| `long_121_365_days` | `19TNxbw6-ymHmUE2dkrSLyvKwYtjP4H5c` |

Inside that dedicated folder, each delivery uses
`YEAR/MONTH/run_<RUN_ID>_<TIMESTAMP>/` and contains the CSV, Excel, and a
manifest with run/dates/frequency/row counts/timestamps/checksums/local paths
and remote paths. Per-artifact state and checksums make retries idempotent; an
already verified matching remote artifact is reused. No old Drive exports are
deleted. Upload failure is independent of collection status, keeps all local
files, and is retried independently on later dispatcher ticks when rclone is
configured. It can also be retried in the UI or with:

```bash
scripts/ota-drive retry-pending
```

The Drive backend is local `rclone`; ChatGPT credentials are not used. If
needed, install it explicitly with `sudo apt install rclone`, then perform the
one-time browser authentication:

```bash
scripts/ota-drive configure
```

Credentials remain only in `~/.config/rclone/rclone.conf`. Test actual
read/write access to every folder, or inspect non-secret status, with:

```bash
scripts/ota-drive test
scripts/ota-drive status
```

## P. Enabling concurrency after the RAM upgrade

Keep `OTA_MAX_CONCURRENT_WORKERS=1` on the current approximately 8 GB machine.
More memory is never detected as permission to increase it. After a 32 GB
upgrade, stop the three Streamlit listeners temporarily so the port checks can
confirm 8501–8503 are available, then run:

```bash
scripts/ota-scheduler preflight
scripts/ota-scheduler enable-concurrency
```

Enablement requires at least 28 GB usable RAM, nonzero swap, sufficient disk,
all instance directories, all three free ports, and the explicit marker written
only by the successful enable command. Only then may
`OTA_MAX_CONCURRENT_WORKERS=3` be accepted. Apply
`systemd/ota-scraper-resources-32gb.conf.disabled` as a service drop-in only
afterward for `MemoryHigh=5G`, `MemoryMax=7G`, `TasksMax=512`,
`Restart=on-failure`, and `RestartSec=30`.

## Q. Consolidated annual CSV

The workers never share a database. Create the latest annual view separately:

```bash
.venv/bin/python tools/consolidate_latest_full_year.py
```

The consolidation process opens each instance database in SQLite read-only
mode, selects that instance's latest successfully exported complete run, and
reads only the CSV path recorded inside that instance's own exports directory.
It deduplicates on source, canonical hotel identity, check-in, checkout, adults,
and currency, retaining the newest collection timestamp. The output is an
atomic UTF-8 BOM file named
`/home/jf/Downloads/Orlando_latest_full_year_<TIMESTAMP>.csv` and includes the
collection instance, source run ID, collection timestamp, and date bucket.
