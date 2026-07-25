from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil

from services.job_runner import atomic_write_json
from services.operational_status import read_json_object, update_operational_status
from services.schedule_config import disable_schedule, paris_now, record_schedule_event
from services.scheduled_instances import get_scheduled_instance


ACTIVE_STATUSES = {"acknowledged", "starting", "running", "stopping"}
COMPLETED_STATUSES = {
    "completed",
    "completed_all_dates",
    "completed_with_failed_dates",
    "completed_with_blocked_dates",
    "completed_with_partial_results",
    "completed_with_zero_results",
}


def _active_scheduled_worker(
    instance_id: str,
    data_dir: Path,
) -> dict[str, Any] | None:
    payload = read_json_object(
        data_dir / "status" / "scheduled_run.pid"
    )
    try:
        pid = int(payload["pid"])
        process = psutil.Process(pid)
        command = " ".join(process.cmdline())
    except (KeyError, TypeError, ValueError, psutil.Error, OSError):
        return None
    if (
        str(payload.get("instance_id") or "") != instance_id
        or "tools/ota_scheduled_run.py" not in command
        or instance_id not in command
    ):
        return None
    return {
        "pid": pid,
        "job_id": str(payload.get("job_id") or ""),
        "create_time": process.create_time(),
    }


def _launch_stop_monitor(
    instance_id: str,
    data_dir: Path,
    worker: dict[str, Any],
) -> int:
    log_path = data_dir / "logs" / "stop_monitor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [
                sys.executable,
                str(
                    Path(__file__).resolve().parents[1]
                    / "tools"
                    / "ota_stop_monitor.py"
                ),
                "--instance",
                instance_id,
                "--data-dir",
                str(data_dir.resolve()),
                "--pid",
                str(worker["pid"]),
                "--create-time",
                str(worker["create_time"]),
                "--job-id",
                str(worker.get("job_id") or ""),
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    return int(process.pid)


def _stopped_run_id(data_dir: Path) -> int | None:
    status = read_json_object(
        data_dir / "status" / "current_job_status.json"
    )
    for value in (
        status.get("current_run_id"),
        status.get("run_id"),
        status.get("database_run_id"),
    ):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    checkpoints = [data_dir / "checkpoints" / "current_run_resume.json"]
    checkpoints.extend(
        sorted(
            (data_dir / "checkpoints").glob("*_resume.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    )
    for checkpoint in checkpoints:
        try:
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            return int(payload["run_id"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def finalize_stopped_run(
    instance_id: str,
    *,
    data_dir: str | Path,
    run_id: int | None = None,
) -> dict[str, Any]:
    """Finalize and export one stopped run after its owned worker has exited."""

    resolved_dir = Path(data_dir)
    database_path = resolved_dir / "hotel_price_collector.sqlite"
    selected_run_id = run_id or _stopped_run_id(resolved_dir)
    if selected_run_id is None or not database_path.is_file():
        return {
            "status": "no_run_to_finalize",
            "run_id": selected_run_id,
        }
    with sqlite3.connect(database_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "select id, status from collection_runs where id = ?",
            (int(selected_run_id),),
        ).fetchone()
        if row is None:
            return {
                "status": "run_not_found",
                "run_id": int(selected_run_id),
            }
        if str(row["status"] or "") not in COMPLETED_STATUSES:
            connection.execute(
                """
                update collection_runs
                set status = 'stopped_by_user',
                    completed_at = coalesce(completed_at, ?)
                where id = ?
                """,
                (paris_now().isoformat(timespec="seconds"), int(selected_run_id)),
            )
            connection.commit()

    from database import db
    from services.run_exports import automatic_export_run_csv

    previous_database = db.SQLITE_DB_PATH
    try:
        db.SQLITE_DB_PATH = database_path
        export = automatic_export_run_csv(
            int(selected_run_id),
            database_path=database_path,
            instance_id=instance_id,
            copy_to_downloads=True,
        )
    finally:
        db.SQLITE_DB_PATH = previous_database
    status_payload = update_operational_status(
        instance_id,
        data_dir=resolved_dir,
        status="stopped_by_user",
        current_message="Stopped by user",
        stop_requested=True,
    )
    return {
        "status": "stopped_by_user",
        "run_id": int(selected_run_id),
        "export": export,
        "status_payload": status_payload,
    }


def request_instance_stop(
    instance_id: str,
    *,
    data_dir: str | Path | None = None,
    source: str,
    in_process_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one isolated stop request and disable future scheduled cycles.

    Process escalation remains with the existing owner: Streamlit owns its
    multiprocessing child, while the persistent supervisor owns its process
    group.  Scheduled workers also observe the same durable cancellation file.
    """

    definition = get_scheduled_instance(instance_id)
    resolved_dir = Path(data_dir or definition.data_dir)
    status_dir = resolved_dir / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    current_status = read_json_object(status_dir / "current_job_status.json")
    scheduled_worker = _active_scheduled_worker(
        instance_id,
        resolved_dir,
    )
    job_id = str(
        (in_process_job or {}).get("id")
        or current_status.get("job_id")
        or current_status.get("active_job_id")
        or (scheduled_worker or {}).get("job_id")
        or ""
    )
    requested_at = paris_now().isoformat(timespec="seconds")

    disable_schedule(
        instance_id,
        data_dir=resolved_dir,
        modified_by=f"stop:{source}",
    )
    cancel_payload = {
        "request_id": str(uuid4()),
        "instance_id": instance_id,
        "job_id": job_id,
        "cancel_requested": True,
        "stop_requested": True,
        "reason": "user",
        "stop_source": source,
        "stop_requested_at": requested_at,
        "updated_at": requested_at,
    }
    atomic_write_json(status_dir / "cancel_request.json", cancel_payload)

    if in_process_job is not None:
        stop_event = in_process_job.get("stop_event")
        if stop_event is not None:
            try:
                stop_event.set(source)
            except TypeError:
                stop_event.set()
        in_process_job["stop_requested_at"] = time.monotonic()

    # The per-instance supervisor consumes this exact request and performs its
    # bounded cooperative -> SIGTERM escalation for only its owned child group.
    atomic_write_json(
        status_dir / "worker_request.json",
        {
            "request_id": str(uuid4()),
            "job_id": job_id,
            "instance_id": instance_id,
            "action": "stop",
            "timestamp": requested_at,
            "stop_source": source,
        },
    )
    active = (
        bool(in_process_job)
        or scheduled_worker is not None
        or str(current_status.get("status") or "") in ACTIVE_STATUSES
    )
    monitor_pid = None
    if scheduled_worker is not None and not in_process_job:
        monitor_pid = _launch_stop_monitor(
            instance_id,
            resolved_dir,
            scheduled_worker,
        )
    status_payload = update_operational_status(
        instance_id,
        data_dir=resolved_dir,
        status="stopping" if active else current_status.get("status") or "idle",
        current_message="Stop requested…",
        stop_requested=True,
        stop_requested_at=requested_at,
        stop_source=source,
        scheduler_enabled=False,
    )
    record_schedule_event(
        instance_id,
        "stop_requested",
        data_dir=resolved_dir,
        stop_source=source,
        job_id=job_id or None,
    )
    return {
        "status": "stop_requested" if active else "scheduler_disabled",
        "active_worker": active,
        "job_id": job_id or None,
        "stop_requested_at": requested_at,
        "stop_monitor_pid": monitor_pid,
        "status_payload": status_payload,
    }
