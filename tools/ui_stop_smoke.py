from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


BASE_DIR = Path(__file__).resolve().parents[1]


def choose_selectbox(page: Page, index: int, option: str) -> None:
    selectbox = page.locator("[data-testid=stSelectbox]").nth(index)
    combobox = selectbox.get_by_role("combobox")
    combobox.click()
    combobox.fill(option)
    combobox.press("Enter")
    page.wait_for_timeout(500)
    if selectbox.get_by_role("combobox").input_value() != option:
        raise RuntimeError(f"Selectbox {index} did not accept {option!r}")


def status_payload() -> dict:
    path = BASE_DIR / "data" / "instances" / "instance_1" / "status" / "current_job_status.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def run(url: str) -> dict:
    diagnostic_dir = BASE_DIR / "data" / "diagnostics" / f"ui_stop_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    diagnostic_dir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    events: list[dict] = []

    def record(event: str, **details) -> None:
        events.append({"elapsed_seconds": round(time.monotonic() - started, 2), "event": event, **details})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chromium")
        page = browser.new_page(viewport={"width": 1440, "height": 1400})
        page.set_default_timeout(10_000)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_selector("[data-testid=stSelectbox]", timeout=20_000)
            page.wait_for_timeout(2_000)
            record("interface_loaded", title=page.title())

            choose_selectbox(page, 1, "Demo")
            record("source_selected", source="Demo")

            date_inputs = page.locator("[data-testid=stDateInput]")
            start_value = date_inputs.nth(0).locator("input").input_value()
            end_input = date_inputs.nth(1).locator("input")
            end_input.fill(start_value)
            end_input.press("Enter")
            page.wait_for_timeout(1_000)
            actual_dates = [
                page.locator("[data-testid=stDateInput]").nth(index).locator("input").input_value()
                for index in range(2)
            ]
            if actual_dates[0] != actual_dates[1]:
                raise RuntimeError(f"Could not constrain UI smoke test to one date: {actual_dates}")
            record("one_date_selected", dates=actual_dates)

            choose_selectbox(page, 4, "Custom number")
            custom_max = page.get_by_label("Custom maximum hotels")
            custom_max.fill("500")
            custom_max.press("Enter")
            page.wait_for_timeout(500)
            record("bounded_property_limit_selected", maximum=500)

            resume = page.get_by_label("Resume incomplete batch")
            if resume.is_checked():
                resume.uncheck(force=True)
                page.wait_for_timeout(500)

            start_button = page.get_by_role("button", name="Start collection", exact=True)
            start_button.click()
            stop_button = page.get_by_role("button", name="Stop collection", exact=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if stop_button.is_enabled():
                    break
                page.wait_for_timeout(100)
            if not stop_button.is_enabled():
                raise RuntimeError("Stop button did not become enabled after starting the worker")
            status_at_start = status_payload()
            record("worker_started", job_id=status_at_start.get("job_id"), status=status_at_start.get("status"))

            page.wait_for_timeout(700)
            stop_clicked_at = time.monotonic()
            stop_button.click()
            record("stop_clicked")

            terminal = {}
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                terminal = status_payload()
                if terminal.get("status") in {"stopped_by_user", "stopped_by_user_with_partial_results"}:
                    break
                page.wait_for_timeout(200)
            stop_latency = round(time.monotonic() - stop_clicked_at, 2)
            if terminal.get("status") not in {"stopped_by_user", "stopped_by_user_with_partial_results"}:
                raise RuntimeError(f"Worker did not reach a stopped-by-user state: {terminal}")
            record("worker_stopped", latency_seconds=stop_latency, status=terminal.get("status"))

            response = page.request.head(url, timeout=10_000)
            record("interface_still_responsive", http_status=response.status)
        finally:
            page.close()
            browser.close()

    database = BASE_DIR / "data" / "instances" / "instance_1" / "hotel_price_collector.sqlite"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        latest_demo = connection.execute(
            "SELECT id, status FROM collection_runs WHERE source = 'Demo' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        rows = (
            connection.execute("SELECT COUNT(*) FROM hotel_price_results WHERE collection_run_id = ?", (latest_demo[0],)).fetchone()[0]
            if latest_demo
            else 0
        )

    payload = {
        "result": "passed",
        "url": url,
        "events": events,
        "final_status": status_payload(),
        "sqlite_integrity": integrity,
        "latest_demo_run": {"id": latest_demo[0], "status": latest_demo[1], "rows": rows} if latest_demo else None,
    }
    output = diagnostic_dir / "ui_stop_smoke.json"
    output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    payload["diagnostic_file"] = str(output)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Exercise the real Streamlit Start and Stop controls with one bounded Demo date.")
    parser.add_argument("--url", default="http://127.0.0.1:8501/")
    args = parser.parse_args()
    print(json.dumps(run(args.url), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
