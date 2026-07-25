from __future__ import annotations

import fcntl
import json
from datetime import date
from pathlib import Path
from typing import Any

from services.job_runner import atomic_write_json
from services.schedule_config import (
    TIMEZONE_NAME,
    load_schedule,
    next_scheduled_run,
    paris_now,
    read_schedule_state,
    resolved_dates_for_schedule,
)
from services.scheduled_instances import get_scheduled_instance


REQUIRED_OPERATIONAL_FIELDS: dict[str, Any] = {
    "scheduler_enabled": False,
    "runs_per_day": None,
    "next_scheduled_run": None,
    "schedule_timezone": TIMEZONE_NAME,
    "schedule_start_date": None,
    "schedule_end_date": None,
    "stop_requested": False,
    "stop_requested_at": None,
    "stop_source": None,
    "local_export_status": None,
    "local_export_path": None,
    "local_export_rows": 0,
    "local_export_bytes": 0,
    "google_drive_upload_status": None,
    "google_drive_folder_id": None,
    "google_drive_remote_filename": None,
    "google_drive_remote_bytes": None,
    "google_drive_upload_error": None,
    "workspace_requested": None,
    "workspace_detected": None,
    "browser_window_id": None,
    "browser_window_workspace": None,
    "workspace_move_status": None,
}


def read_json_object(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def update_operational_status(
    instance_id: str,
    *,
    data_dir: str | Path | None = None,
    anchor_date: date | None = None,
    **updates: Any,
) -> dict[str, Any]:
    """Merge scheduler/export/Drive/workspace diagnostics atomically per instance."""

    definition = get_scheduled_instance(instance_id)
    resolved_dir = Path(data_dir or definition.data_dir)
    status_path = resolved_dir / "status" / "current_job_status.json"
    lock_path = status_path.with_suffix(".json.operational.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    now = paris_now()
    schedule = load_schedule(instance_id, data_dir=resolved_dir, now=now)
    state = read_schedule_state(instance_id, data_dir=resolved_dir)
    start, end = resolved_dates_for_schedule(
        schedule,
        anchor_date or now.date(),
    )
    following = next_scheduled_run(schedule, state, now=now)
    scheduler_fields = {
        "scheduler_enabled": bool(schedule["enabled"]),
        "runs_per_day": schedule.get("runs_per_day"),
        "next_scheduled_run": (
            following.isoformat(timespec="seconds") if following else None
        ),
        "schedule_timezone": schedule["timezone"],
        "schedule_start_date": start.isoformat(),
        "schedule_end_date": end.isoformat(),
        "google_drive_folder_id": schedule["drive_folder_id"],
    }
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        payload = {
            **REQUIRED_OPERATIONAL_FIELDS,
            **read_json_object(status_path),
            **scheduler_fields,
            **updates,
            "instance_id": instance_id,
            "last_updated_at": now.isoformat(timespec="seconds"),
        }
        atomic_write_json(status_path, payload)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return payload
