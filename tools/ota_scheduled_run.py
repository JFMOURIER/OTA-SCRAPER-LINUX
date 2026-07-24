#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.job_runner import CancellationSignal, atomic_write_json
from services.scheduled_instances import get_scheduled_instance


class ScheduledQueue:
    def __init__(self, job_id: str, log_file: Path) -> None:
        self.job_id = job_id
        self.instance_log_file = str(log_file.resolve())
        self.log_file = log_file

    def put(self, item: Any) -> None:
        if not isinstance(item, tuple) or len(item) != 2:
            return
        kind, payload = item
        if kind != "log":
            return
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{payload}\n")
            handle.flush()


def _truthy(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"[{datetime.now().isoformat(sep=' ', timespec='seconds')}] "
            f"{message}\n"
        )
        handle.flush()


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "usage: ota_scheduled_run.py "
            "{near_30_days|medium_31_120_days|long_121_365_days}",
            file=sys.stderr,
        )
        return 2
    definition = get_scheduled_instance(sys.argv[1])
    expected_data_dir = definition.data_dir.resolve()
    configured_instance = os.getenv("INSTANCE_ID", definition.instance_id)
    configured_data_dir = Path(
        os.getenv("INSTANCE_DATA_DIR", str(expected_data_dir))
    ).resolve()
    if configured_instance != definition.instance_id:
        raise RuntimeError(
            f"INSTANCE_ID={configured_instance!r} does not match "
            f"{definition.instance_id!r}"
        )
    if configured_data_dir != expected_data_dir:
        raise RuntimeError(
            f"INSTANCE_DATA_DIR={configured_data_dir} does not match "
            f"{expected_data_dir}"
        )

    os.environ.update(
        {
            "INSTANCE_ID": definition.instance_id,
            "INSTANCE_NAME": definition.instance_id.replace("_", " ").title(),
            "INSTANCE_PORT": str(definition.port),
            "INSTANCE_DATA_DIR": str(expected_data_dir),
            "INSTANCE_CITY": os.getenv("INSTANCE_CITY", "Orlando"),
            "INSTANCE_SOURCE": os.getenv("INSTANCE_SOURCE", "Booking.com"),
            "INSTANCE_DATE_BUCKET": definition.date_bucket,
            "DB_BACKEND": "sqlite",
            "OTA_SCHEDULED_RUN": "1",
        }
    )
    if not _truthy("OTA_BROWSER_HEADLESS", True):
        os.environ["DISPLAY"] = os.getenv("DISPLAY", definition.display)

    import app

    app.INSTANCE_CONFIG.ensure_directories()
    log_file = (
        app.LOG_DIR
        / f"scheduled_{definition.instance_id}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    lock_path = app.INSTANCE_CONFIG.scheduled_lock_file
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(
                lock_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            _log(log_file, "scheduled_run_skipped_previous_run_active")
            return 0
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(
            f"pid={os.getpid()} instance={definition.instance_id} "
            f"started={datetime.now().isoformat(sep=' ', timespec='seconds')}\n"
        )
        lock_handle.flush()
        os.fsync(lock_handle.fileno())

        today = datetime.now().date()
        checkin_start, checkin_end = definition.resolve_window(today)
        job_id = f"scheduled-{definition.instance_id}-{uuid4()}"
        atomic_write_json(
            app.INSTANCE_CONFIG.pid_file,
            {
                "pid": os.getpid(),
                "job_id": job_id,
                "instance_id": definition.instance_id,
                "started_at": datetime.now().isoformat(
                    sep=" ",
                    timespec="seconds",
                ),
                "resolved_checkin_start": checkin_start.isoformat(),
                "resolved_checkin_end": checkin_end.isoformat(),
            },
        )
        atomic_write_json(
            app.STATUS_DIR / "scheduled_resolution.json",
            {
                "job_id": job_id,
                "instance_id": definition.instance_id,
                "date_bucket": definition.date_bucket,
                "resolved_at": datetime.now().isoformat(
                    sep=" ",
                    timespec="seconds",
                ),
                "today": today.isoformat(),
                "checkin_start": checkin_start.isoformat(),
                "checkin_end": checkin_end.isoformat(),
            },
        )
        _log(
            log_file,
            f"resolved_dynamic_window {checkin_start} to {checkin_end}",
        )
        config = app.CollectionConfig(
            source=os.getenv("INSTANCE_SOURCE", "Booking.com"),
            city_or_region=os.getenv("INSTANCE_CITY", "Orlando"),
            checkin_start=checkin_start,
            checkin_end=checkin_end,
            nights=max(1, int(os.getenv("OTA_SCHEDULED_NIGHTS", "1"))),
            adults=max(1, int(os.getenv("OTA_SCHEDULED_ADULTS", "2"))),
            currency=os.getenv("OTA_SCHEDULED_CURRENCY", "USD"),
            max_hotels=max(
                1,
                int(os.getenv("OTA_SCHEDULED_MAX_HOTELS", "250")),
            ),
            headless=_truthy("OTA_BROWSER_HEADLESS", True),
            collect_all_available=False,
            max_scroll_minutes=max(
                1,
                int(os.getenv("OTA_SCHEDULED_MAX_SCROLL_MINUTES", "10")),
            ),
            selected_star_ratings=(1, 2, 3, 4, 5),
            include_unknown_star_rating=True,
            debug_mode=False,
            screenshots_enabled=False,
            fast_mode=False,
            performance_mode="balanced",
            block_images_and_fonts=True,
            test_mode=False,
            db_backend="sqlite",
            hotels_only=True,
            disable_filters_during_complete_collection=False,
            ultra_reliable_loading_mode=False,
            resume_previous_run=False,
            batch_mode="single",
            custom_block_days=1,
            max_parallel_workers=1,
            retry_failed_dates_automatically=True,
            max_retries_per_date=max(
                1,
                int(os.getenv("OTA_SCHEDULED_MAX_RETRIES", "3")),
            ),
            continue_if_date_fails=True,
            auto_export_partial_excel=False,
            partial_export_frequency="every_25_dates",
        )
        queue = ScheduledQueue(job_id, log_file)
        stop_signal = CancellationSignal(
            threading.Event(),
            app.CANCEL_FILE,
            job_id,
        )
        stop_signal.reset()
        app.run_background_job_with_fatal_guard(
            app.run_resilient_collection_job,
            config,
            stop_signal,
            queue,
            job_id,
            str(log_file.resolve()),
        )
        status: dict[str, Any] = {}
        try:
            status = json.loads(
                app.STATUS_FILE.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError):
            pass
        final = str(status.get("status") or "")
        atomic_write_json(
            app.INSTANCE_CONFIG.pid_file,
            {
                "pid": os.getpid(),
                "job_id": job_id,
                "instance_id": definition.instance_id,
                "finished_at": datetime.now().isoformat(
                    sep=" ",
                    timespec="seconds",
                ),
                "status": final,
            },
        )
        if final in {
            "scheduled_run_skipped_previous_run_active",
            "scheduled_run_skipped_host_concurrency_limit",
            "concurrency_upgrade_not_ready",
        }:
            return 0
        if final in {
            "fatal_startup_error",
            "application_exception",
            "fatal_error_with_partial_results",
        }:
            return 70
        return 0
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
