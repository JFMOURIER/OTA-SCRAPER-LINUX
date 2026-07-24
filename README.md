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

## M. Isolated scheduled instances

Prepare the directory layout without deleting or migrating existing runtime
files:

```bash
scripts/ota-scheduler prepare
scripts/ota-scheduler preflight
```

The definitions are:

| Instance | Port | Dynamic stay window | Headless display fallback |
| --- | ---: | --- | --- |
| `near_30_days` | 8501 | today through today + 30 days | `:101` |
| `medium_31_120_days` | 8502 | today + 31 through today + 120 days | `:102` |
| `long_121_365_days` | 8503 | today + 121 through today + 365 days | `:103` |

Each root at `data/instances/<INSTANCE_ID>/` owns its SQLite database, exports,
partials, status, logs, debug files, checkpoints, browser profiles, heartbeat,
PID, and locks. No partial or browser-profile path is shared. Absolute stay
dates are resolved immediately before each scheduled run and are stored in its
`collection_runs` record.

Install the supplied units with:

```bash
scripts/ota-scheduler install
scripts/ota-scheduler enable-timers
```

The timers use Europe/Paris host time:

| Timer | Schedule |
| --- | --- |
| `ota-scraper-near.timer` | minute 05 every hour |
| `ota-scraper-medium.timer` | 00:20 and 12:20 daily |
| `ota-scraper-long.timer` | 01:35 daily |

Timers use `Persistent=false`, so missed invocations are not queued. A second
invocation of the same instance logs
`scheduled_run_skipped_previous_run_active` and exits successfully. The
per-instance lock prevents overlap; the host slot semaphore limits aggregate
workers. Scheduled Playwright runs are headless by default. If visible Chromium
is required, install/start `ota-xvfb@101.service`,
`ota-xvfb@102.service`, and `ota-xvfb@103.service`, then set
`OTA_BROWSER_HEADLESS=0`; the instances never share desktop display `:0`.

## N. Enabling concurrency after the RAM upgrade

Keep `/etc/ota-scraper/scheduler.conf` at:

```text
OTA_MAX_CONCURRENT_WORKERS=1
```

After the 32 GB upgrade, stop the three Streamlit listeners temporarily so the
port checks can confirm 8501, 8502, and 8503 are available, then run:

```bash
scripts/ota-scheduler preflight
scripts/ota-scheduler enable-concurrency
```

Enablement requires at least 28 GB total usable RAM, nonzero swap, sufficient
disk space, all three configured instance directories, and all three ports
available. Only after that command succeeds may
`OTA_MAX_CONCURRENT_WORKERS=3` be set. Copy
`systemd/ota-scraper-resources-32gb.conf.disabled` into a service drop-in only
after the upgrade to apply `MemoryHigh=5G`, `MemoryMax=7G`, `TasksMax=512`,
`Restart=on-failure`, and `RestartSec=30`. The schedules are staggered so their
normal launches do not begin in the same minute.

## O. Consolidated annual CSV

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
