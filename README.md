# OTA-SCRAPER-LINUX

## A. Project description

OTA-SCRAPER-LINUX is the Ubuntu/Linux version of the OTA hotel price scraper. It keeps the Streamlit dashboard, scraping workflow, local SQLite storage, Excel exports, checkpoints, screenshots, and visible logs from the working OTA Scraper while using Linux-compatible paths and Playwright Chromium setup.

The app can be launched with:

```bash
streamlit run app.py
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
INSTANCE_DATA_DIR=data/instances/period_1
```

Only add real API keys or database passwords to `.env`. Do not commit `.env`; it is ignored by git.

## H. Launch the application

Recommended launcher:

```bash
chmod +x run_linux.sh
./run_linux.sh
```

Manual launch:

```bash
source .venv/bin/activate
streamlit run app.py
```

Use another port if `8501` is already busy:

```bash
streamlit run app.py --server.port 8502
```

## I. Troubleshooting

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
data/instances/period_1/logs/
data/instances/period_1/status/
data/instances/period_1/debug/
```

CAPTCHA or access restriction: the scraper is designed to stop safely, save partial results where possible, and mark the date as blocked. It does not bypass website security controls.

## J. GitHub update workflow

Use this workflow when updating the Linux project from the Linux tower PC:

```bash
cd ~/OTA-SCRAPER-LINUX
git pull
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
streamlit run app.py
```

To save your own code changes:

```bash
git status
git add app.py collectors services database tools requirements.txt README.md .gitignore .env.example run_linux.sh
git commit -m "Describe your Linux scraper update"
git push
```

Never commit `.env`, `data/`, SQLite databases, Excel exports, CSV files, screenshots, logs, debug files, checkpoints, or browser profiles.
