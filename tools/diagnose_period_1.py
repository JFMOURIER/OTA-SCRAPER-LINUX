from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
INSTANCE_DIR = BASE_DIR / "data" / "instances" / "period_1"
STATUS_DIR = INSTANCE_DIR / "status"
LOG_DIR = INSTANCE_DIR / "logs"
SQLITE_PATH = INSTANCE_DIR / "hotel_price_collector.sqlite"


def print_check(name: str, ok: bool, detail: str = "") -> bool:
    marker = "OK" if ok else "ERROR"
    print(f"[{marker}] {name}: {detail}")
    return ok


def latest_file(folder: Path, pattern: str = "*") -> Path | None:
    if not folder.exists():
        return None
    files = [path for path in folder.glob(pattern) if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def check_import(module_name: str) -> tuple[bool, str]:
    try:
        __import__(module_name)
        return True, "installed"
    except Exception as exc:
        return False, str(exc)


def check_chromium() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
        if executable and Path(executable).exists():
            return True, str(executable)
        return False, f"Chromium executable not found: {executable}"
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    failures = 0
    failures += not print_check("Project folder exists", BASE_DIR.exists(), str(BASE_DIR))
    print(f"Python version: {sys.version}")
    venv_detected = bool(os.getenv("VIRTUAL_ENV")) or "venv" in str(sys.executable).lower()
    failures += not print_check("Venv detected", venv_detected, sys.executable)

    ok, detail = check_import("streamlit")
    failures += not print_check("Streamlit installed", ok, detail)
    ok, detail = check_import("playwright")
    failures += not print_check("Playwright installed", ok, detail)
    ok, detail = check_chromium()
    failures += not print_check("Chromium installed", ok, detail)

    failures += not print_check("Instance folder exists", INSTANCE_DIR.exists(), str(INSTANCE_DIR))
    print(f"SQLite database path: {SQLITE_PATH}")

    latest_log = latest_file(LOG_DIR)
    print(f"Latest log file: {latest_log if latest_log else 'none'}")
    current_status_path = STATUS_DIR / "current_job_status.json"
    latest_status = latest_file(STATUS_DIR)
    print(f"Latest status file: {current_status_path if current_status_path.exists() else latest_status if latest_status else 'none'}")
    current_status = read_json(current_status_path)
    last_error = read_json(STATUS_DIR / "last_error.json")
    current_error = current_status.get("last_error")
    current_status_text = str(current_status.get("status") or "")
    status_is_error = bool(current_error) or current_status_text.startswith("fatal") or current_status_text in {"failed", "preflight_failed"}
    if current_error:
        print(f"Latest error: {current_error}")
        if current_status.get("last_traceback_file"):
            print(f"Latest traceback file: {current_status['last_traceback_file']}")
    elif status_is_error and last_error.get("error_message"):
        print(f"Latest error: {last_error.get('error_message')}")
        if last_error.get("traceback_file"):
            print(f"Latest traceback file: {last_error['traceback_file']}")
    else:
        print("Latest error: none")

    result = subprocess.run(
        [sys.executable, str(BASE_DIR / "tools" / "test_collector_options.py")],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    failures += not print_check("CollectorOptions test result", result.returncode == 0, f"exit code {result.returncode}")

    if failures:
        print(f"Diagnostics completed with {failures} failing check(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
