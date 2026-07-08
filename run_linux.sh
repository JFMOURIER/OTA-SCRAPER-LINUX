#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "[OTA-SCRAPER-LINUX] Starting Streamlit dashboard"
echo "[OTA-SCRAPER-LINUX] Project folder: $(pwd)"

if [[ ! -d ".venv" ]]; then
  echo "[OTA-SCRAPER-LINUX] Creating Python virtual environment in .venv"
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "[OTA-SCRAPER-LINUX] Installing Python requirements"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "[OTA-SCRAPER-LINUX] Checking Playwright Chromium"
python -m playwright install --with-deps chromium

if [[ ! -f ".env" && -f ".env.example" ]]; then
  echo "[OTA-SCRAPER-LINUX] Creating .env from .env.example"
  cp .env.example .env
fi

mkdir -p data

echo "[OTA-SCRAPER-LINUX] Launching: streamlit run app.py"
streamlit run app.py
