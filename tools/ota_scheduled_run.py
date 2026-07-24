#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.job_runner import CancellationSignal, atomic_write_json
from services.schedule_config import (
    claim_launch_request,
    frequency_configuration,
    launch_request_path,
    mark_worker_finished,
    record_schedule_event,
    update_schedule_state,
)
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


def _log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"[{datetime.now().isoformat(sep=' ', timespec='seconds')}] "
            f"{message}\n"
        )
        handle.flush()


def _resolved_data_dir(instance_id: str, requested: str | None) -> Path:
    definition = get_scheduled_instance(instance_id)
    if not requested:
        return definition.data_dir.resolve()
    if os.getenv("OTA_DISPOSABLE_TEST") != "1":
        raise RuntimeError(
            "A data-directory override is allowed only with "
            "OTA_DISPOSABLE_TEST=1"
        )
    resolved = Path(requested).resolve()
    try:
        resolved.relative_to(Path("/tmp").resolve())
    except ValueError as exc:
        raise RuntimeError(
            "Disposable scheduler data must remain under /tmp"
        ) from exc
    return resolved


def _build_config(app: Any, request: dict[str, Any]) -> Any:
    template = dict(request["collection_template"])
    performance = str(template.get("performance_mode", "balanced"))
    headless = str(template.get("browser_mode", "headless")) == "headless"
    return app.CollectionConfig(
        source=str(template["source"]),
        city_or_region=str(template["city_or_region"]),
        checkin_start=date.fromisoformat(
            str(request["resolved_start_date"])
        ),
        checkin_end=date.fromisoformat(str(request["resolved_end_date"])),
        nights=max(1, int(template.get("nights", 1))),
        adults=max(1, int(template.get("adults", 2))),
        currency=str(template.get("currency", "USD")),
        max_hotels=max(1, int(template.get("max_hotels", 250))),
        headless=headless,
        collect_all_available=bool(
            template.get("collect_all_available", False)
        ),
        max_scroll_minutes=max(
            1,
            int(template.get("max_scroll_minutes", 10)),
        ),
        selected_star_ratings=tuple(
            int(value)
            for value in template.get(
                "selected_star_ratings",
                (1, 2, 3, 4, 5),
            )
        ),
        include_unknown_star_rating=bool(
            template.get("include_unknown_star_rating", True)
        ),
        debug_mode=performance == "debug",
        screenshots_enabled=performance == "debug",
        fast_mode=False,
        performance_mode=performance,
        block_images_and_fonts=performance == "balanced",
        test_mode=bool(template.get("test_mode", False)),
        db_backend="sqlite",
        hotels_only=bool(template.get("hotels_only", True)),
        disable_filters_during_complete_collection=False,
        ultra_reliable_loading_mode=False,
        resume_previous_run=False,
        batch_mode="single",
        custom_block_days=1,
        max_parallel_workers=1,
        retry_failed_dates_automatically=bool(
            template.get("retry_failed_dates_automatically", True)
        ),
        max_retries_per_date=max(
            1,
            int(template.get("max_retries_per_date", 3)),
        ),
        continue_if_date_fails=bool(
            template.get("continue_if_date_fails", True)
        ),
        auto_export_partial_excel=bool(
            template.get("auto_export_partial_excel", False)
        ),
        partial_export_frequency=str(
            template.get("partial_export_frequency", "every_25_dates")
        ),
        rooms=max(1, int(template.get("rooms", 1))),
    )


def _latest_run_id(app: Any, request: dict[str, Any]) -> int | None:
    status: dict[str, Any] = {}
    try:
        status = json.loads(app.STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    if status.get("run_id") is not None:
        return int(status["run_id"])
    try:
        from database.db import fetch_collection_runs

        for run in fetch_collection_runs(limit=20, backend="sqlite"):
            if run.get("schedule_slot") == request.get("schedule_slot"):
                return int(run["id"])
    except Exception:
        pass
    return None


def _requeue_after_capacity(
    instance_id: str,
    data_dir: Path,
    request: dict[str, Any],
) -> None:
    slot_key = str(request["schedule_slot"])
    pending = {
        "slot_key": slot_key,
        "scheduled_for": request["scheduled_for"],
        "first_due_at": request.get("requested_at"),
        "resolved_start_date": request["resolved_start_date"],
        "resolved_end_date": request["resolved_end_date"],
        "date_mode": request["date_mode"],
        "frequency_configuration": request["frequency_configuration"],
    }

    def update(state: dict[str, Any]) -> dict[str, Any]:
        state["pending_due_run"] = pending
        state["dispatched_slot_keys"] = [
            value
            for value in state.get("dispatched_slot_keys") or []
            if value != slot_key
        ]
        state["claimed_slot_keys"] = [
            value
            for value in state.get("claimed_slot_keys") or []
            if value != slot_key
        ]
        state["last_defer_reason"] = (
            "scheduled_run_deferred_host_capacity"
        )
        state["current_worker_state"] = "idle"
        return state

    update_schedule_state(instance_id, update, data_dir=data_dir)


def run(instance_id: str, data_dir: Path) -> int:
    definition = get_scheduled_instance(instance_id)
    os.environ.update(
        {
            "INSTANCE_ID": instance_id,
            "INSTANCE_NAME": instance_id.replace("_", " ").title(),
            "INSTANCE_PORT": str(definition.port),
            "INSTANCE_DATA_DIR": str(data_dir),
            "INSTANCE_CITY": os.getenv("INSTANCE_CITY", "Orlando"),
            "INSTANCE_SOURCE": os.getenv("INSTANCE_SOURCE", "Booking.com"),
            "INSTANCE_DATE_BUCKET": definition.date_bucket,
            "DB_BACKEND": "sqlite",
            "OTA_SCHEDULED_RUN": "1",
        }
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir = data_dir / "logs"
    status_dir = data_dir / "status"
    log_file = (
        log_dir
        / f"scheduled_{instance_id}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    scheduled_lock = status_dir / "scheduled_run.lock"
    scheduled_lock.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = scheduled_lock.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(
                lock_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            _log(log_file, "scheduled_run_skipped_previous_run_active")
            record_schedule_event(
                instance_id,
                "scheduled_run_skipped_previous_run_active",
                data_dir=data_dir,
                skip_or_defer_reason=(
                    "scheduled_run_skipped_previous_run_active"
                ),
            )
            return 0
        request = claim_launch_request(instance_id, data_dir=data_dir)
        if request is None:
            _log(
                log_file,
                "No unclaimed scheduled launch request; refusing duplicate run.",
            )
            return 0
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(
            f"pid={os.getpid()} instance={instance_id} "
            f"slot={request['schedule_slot']} "
            f"started={datetime.now().isoformat(sep=' ', timespec='seconds')}\n"
        )
        lock_handle.flush()
        os.fsync(lock_handle.fileno())
        os.environ.update(
            {
                "INSTANCE_CITY": str(
                    request["collection_template"]["city_or_region"]
                ),
                "INSTANCE_SOURCE": str(
                    request["collection_template"]["source"]
                ),
                "OTA_RESOLVED_START_DATE": str(
                    request["resolved_start_date"]
                ),
                "OTA_RESOLVED_END_DATE": str(
                    request["resolved_end_date"]
                ),
                "INSTANCE_START_DATE": str(
                    request["resolved_start_date"]
                ),
                "INSTANCE_END_DATE": str(
                    request["resolved_end_date"]
                ),
                "OTA_SCHEDULE_DATE_MODE": str(request["date_mode"]),
                "OTA_SCHEDULE_SLOT": str(request["schedule_slot"]),
                "OTA_SCHEDULE_FREQUENCY_JSON": json.dumps(
                    request["frequency_configuration"],
                    sort_keys=True,
                ),
                "OTA_BROWSER_HEADLESS": (
                    "1"
                    if request["collection_template"].get("browser_mode")
                    == "headless"
                    else "0"
                ),
            }
        )
        if os.environ["OTA_BROWSER_HEADLESS"] == "0":
            os.environ["DISPLAY"] = os.getenv("DISPLAY", definition.display)
        import app

        app.INSTANCE_CONFIG.ensure_directories()
        job_id = (
            f"scheduled-{instance_id}-"
            f"{request['schedule_slot']}-{uuid4()}"
        )
        atomic_write_json(
            app.INSTANCE_CONFIG.pid_file,
            {
                "pid": os.getpid(),
                "job_id": job_id,
                "instance_id": instance_id,
                "schedule_slot": request["schedule_slot"],
                "started_at": datetime.now().isoformat(
                    sep=" ",
                    timespec="seconds",
                ),
                "resolved_checkin_start": request["resolved_start_date"],
                "resolved_checkin_end": request["resolved_end_date"],
            },
        )
        record_schedule_event(
            instance_id,
            "scheduled_run_started",
            data_dir=data_dir,
            schedule_slot=request["schedule_slot"],
            resolved_start_date=request["resolved_start_date"],
            resolved_end_date=request["resolved_end_date"],
            date_mode=request["date_mode"],
            frequency_configuration=request["frequency_configuration"],
            worker_pid=os.getpid(),
            host_slot_result="worker_starting",
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
            _build_config(app, request),
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
        run_id = _latest_run_id(app, request)
        if final in {
            "scheduled_run_deferred_host_capacity",
            "scheduled_run_skipped_host_concurrency_limit",
        }:
            _requeue_after_capacity(instance_id, data_dir, request)
            record_schedule_event(
                instance_id,
                "scheduled_run_deferred_host_capacity",
                data_dir=data_dir,
                schedule_slot=request["schedule_slot"],
                resolved_start_date=request["resolved_start_date"],
                resolved_end_date=request["resolved_end_date"],
                date_mode=request["date_mode"],
                frequency_configuration=request["frequency_configuration"],
                worker_pid=os.getpid(),
                host_slot_result="race_lost",
                skip_or_defer_reason=(
                    "scheduled_run_deferred_host_capacity"
                ),
            )
            return 0
        mark_worker_finished(
            instance_id,
            status=final,
            run_id=run_id,
            worker_pid=os.getpid(),
            data_dir=data_dir,
        )
        event = (
            "scheduled_run_completed"
            if final
            in {
                "completed",
                "completed_all_dates",
                "completed_with_failed_dates",
                "completed_with_blocked_dates",
                "completed_with_partial_results",
                "stopped",
                "stopped_by_user",
                "stopped_by_user_with_partial_results",
            }
            else "scheduled_run_failed"
        )
        record_schedule_event(
            instance_id,
            event,
            data_dir=data_dir,
            schedule_slot=request["schedule_slot"],
            resolved_start_date=request["resolved_start_date"],
            resolved_end_date=request["resolved_end_date"],
            date_mode=request["date_mode"],
            frequency_configuration=request["frequency_configuration"],
            run_id=run_id,
            worker_pid=os.getpid(),
            local_export_status=status.get("csv_export_status"),
            drive_upload_status=status.get("drive_upload_status"),
            final_status=final,
        )
        atomic_write_json(
            app.INSTANCE_CONFIG.pid_file,
            {
                "pid": os.getpid(),
                "job_id": job_id,
                "instance_id": instance_id,
                "schedule_slot": request["schedule_slot"],
                "finished_at": datetime.now().isoformat(
                    sep=" ",
                    timespec="seconds",
                ),
                "status": final,
                "run_id": run_id,
            },
        )
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one claimed configurable OTA schedule slot."
    )
    parser.add_argument(
        "instance_id",
        choices=(
            "near_30_days",
            "medium_31_120_days",
            "long_121_365_days",
        ),
    )
    parser.add_argument(
        "--data-dir",
        help="Disposable /tmp data override; requires OTA_DISPOSABLE_TEST=1",
    )
    args = parser.parse_args()
    return run(
        args.instance_id,
        _resolved_data_dir(args.instance_id, args.data_dir),
    )


if __name__ == "__main__":
    raise SystemExit(main())
