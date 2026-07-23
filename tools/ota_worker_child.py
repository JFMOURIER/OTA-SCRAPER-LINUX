#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FileQueue:
    """Queue-compatible sink that never retains worker results in memory."""

    def __init__(self, job_id: str, log_file: Path) -> None:
        self.job_id = job_id
        self.instance_log_file = str(log_file.resolve())
        self.log_file = log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def put(self, item: Any) -> None:
        if not isinstance(item, tuple) or len(item) != 2:
            return
        kind, payload = item
        if kind != "log":
            return
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now().strftime('%H:%M:%S')}] {payload}\n")
            handle.flush()


def build_config(app: Any, payload: dict[str, Any]) -> Any:
    raw = dict(payload["job_config"])
    return app.CollectionConfig(
        source=str(raw["source"]),
        city_or_region=str(raw["city_or_region"]),
        checkin_start=date.fromisoformat(str(raw["checkin_start"])),
        checkin_end=date.fromisoformat(str(raw["checkin_end"])),
        nights=max(1, int(raw.get("nights", 1))),
        adults=max(1, int(raw.get("adults", 2))),
        currency=str(raw.get("currency", "USD")),
        max_hotels=max(1, int(raw["max_hotels"])),
        headless=bool(raw.get("headless", False)),
        collect_all_available=bool(raw.get("collect_all_available", False)),
        max_scroll_minutes=max(1, int(raw.get("max_scroll_minutes", 5))),
        selected_star_ratings=tuple(int(value) for value in raw.get("selected_star_ratings", (1, 2, 3, 4, 5))),
        include_unknown_star_rating=bool(raw.get("include_unknown_star_rating", True)),
        debug_mode=bool(raw.get("debug_mode", False)),
        screenshots_enabled=bool(raw.get("screenshots_enabled", False)),
        fast_mode=bool(raw.get("fast_mode", False)),
        performance_mode=str(raw.get("performance_mode", "balanced")),
        block_images_and_fonts=bool(raw.get("block_images_and_fonts", False)),
        test_mode=bool(raw.get("test_mode", False)),
        db_backend=str(raw.get("db_backend", "sqlite")),
        hotels_only=bool(raw.get("hotels_only", True)),
        disable_filters_during_complete_collection=bool(
            raw.get("disable_filters_during_complete_collection", False)
        ),
        ultra_reliable_loading_mode=bool(raw.get("ultra_reliable_loading_mode", False)),
        resume_previous_run=bool(payload.get("supervisor_resume") or raw.get("resume_previous_run", False)),
        batch_mode="single",
        custom_block_days=1,
        max_parallel_workers=1,
        retry_failed_dates_automatically=bool(raw.get("retry_failed_dates_automatically", True)),
        max_retries_per_date=max(1, int(raw.get("max_retries_per_date", 3))),
        continue_if_date_fails=bool(raw.get("continue_if_date_fails", True)),
        auto_export_partial_excel=bool(raw.get("auto_export_partial_excel", False)),
        partial_export_frequency=str(raw.get("partial_export_frequency", "every_5_dates")),
    )


def load_payload(argument: str) -> dict[str, Any]:
    path = Path(argument)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = json.loads(argument)
    if not isinstance(payload, dict):
        raise ValueError("worker payload must be a JSON object")
    return payload


def main() -> int:
    payload = load_payload(sys.argv[1])
    for key, value in dict(payload.get("env") or {}).items():
        os.environ[str(key)] = str(value)
    import app
    from services.job_runner import CancellationSignal

    config = build_config(app, payload)
    job_id = str(payload.get("job_id") or payload.get("request_id") or "external")
    log_file = app.LOG_DIR / f"instance_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    queue = FileQueue(job_id, log_file)
    stop_signal = CancellationSignal(threading.Event(), app.CANCEL_FILE, job_id)
    stop_signal.reset()
    app.run_background_job_with_fatal_guard(
        app.run_resilient_collection_job,
        config,
        stop_signal,
        queue,
        job_id,
        str(log_file.resolve()),
    )
    status = {}
    try:
        status = json.loads(app.STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    final = str(status.get("status") or "")
    if final == "stopped_resource_limit":
        return 75
    if final in {"fatal_startup_error", "application_exception", "fatal_error_with_partial_results"}:
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
