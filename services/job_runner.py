from __future__ import annotations

import csv
import json
import os
import time
from uuid import uuid4
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


DATE_TERMINAL_STATUSES = {
    "completed",
    "completed_partial",
    "completed_target_reached",
    "completed_results_exhausted",
    "failed_after_retries",
    "blocked_or_access_restricted",
    "skipped_resume",
    "stopped_resource_limit",
    "stopped_by_user",
}

DATE_PROVEN_COMPLETE_STATUSES = {
    "completed_target_reached",
    "completed_results_exhausted",
}


@dataclass(frozen=True, slots=True)
class RetrySettings:
    retry_failed_dates_automatically: bool = True
    max_retries_per_date: int = 3
    retry_delay_seconds: int = 30
    restart_browser_between_retries: bool = True
    continue_if_date_fails: bool = True
    auto_export_partial_excel: bool = True
    partial_export_frequency: str = "every_date"
    resume_from_checkpoint: bool = True


def now_text() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def atomic_write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    last_error: OSError | None = None
    for attempt in range(1, 8):
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(path)
            return path
        except OSError as exc:
            last_error = exc
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            time.sleep(min(0.05 * attempt, 0.5))
    if last_error:
        raise last_error
    return path


def atomic_write_json(path: Path, payload: Any) -> Path:
    return atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=True, default=str))


def partial_paths(partial_dir: Path, stay_date: date) -> tuple[Path, Path]:
    date_part = stay_date.isoformat()
    return (
        partial_dir / f"{date_part}_partial_hotels.json",
        partial_dir / f"{date_part}_partial_hotels.csv",
    )


def save_partial_records(partial_dir: Path, stay_date: date, records: list[dict[str, Any]]) -> dict[str, str | int]:
    json_path, csv_path = partial_paths(partial_dir, stay_date)
    atomic_write_json(json_path, records)
    fieldnames = sorted({key for row in records for key in row.keys()})
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in records:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(csv_path)
    return {"json": str(json_path.resolve()), "csv": str(csv_path.resolve()), "records": len(records)}


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, default=str)
    return value


def load_checkpoint(path: Path, expected_signature: dict[str, Any]) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("signature") != expected_signature:
        return None
    return payload


def build_checkpoint(
    *,
    job_id: str | None,
    run_id: int | None,
    source: str,
    city: str,
    start_date: date,
    end_date: date,
    nights: int,
    planned_dates: list[date],
    signature: dict[str, Any],
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "run_id": run_id,
        "source": source,
        "city": city,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "length_of_stay": nights,
        "total_stay_dates": len(planned_dates),
        "completed_dates": [],
        "failed_dates": [],
        "blocked_dates": [],
        "in_progress_date": None,
        "last_successful_date": None,
        "last_error": None,
        "last_updated_at": now_text(),
        "output_files": {},
        "retry_counts_by_date": {},
        "date_statuses": {},
        "dates": {},
        "signature": signature,
    }


def update_checkpoint_date(
    checkpoint: dict[str, Any],
    *,
    stay_date: date,
    checkout_date: date,
    status: str,
    attempts: int = 0,
    records_collected: int = 0,
    error: str | None = None,
    output_files: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    date_key = stay_date.isoformat()
    now = now_text()
    row = checkpoint.setdefault("date_statuses", {}).setdefault(date_key, {})
    started_at = row.get("started_at") or now
    row.update(
        {
            "checkin_date": date_key,
            "checkout_date": checkout_date.isoformat(),
            "status": status,
            "attempts": attempts,
            "records_collected": records_collected,
            "visible_result_count": (metrics or {}).get("visible_hotel_cards_count") or (metrics or {}).get("final_cards_found"),
            "load_more_clicks": (metrics or {}).get("load_more_clicks", 0),
            "started_at": started_at,
            "completed_at": now if status in DATE_TERMINAL_STATUSES else None,
            "duration_seconds": _duration_seconds(started_at, now),
            "last_error": error,
            "partial_file_path": (output_files or {}).get("partial_json"),
            "saved_to_sqlite": bool((output_files or {}).get("saved_to_sqlite")),
            "exported_to_excel": bool((output_files or {}).get("partial_excel")),
        }
    )
    checkpoint.setdefault("dates", {})[date_key] = {
        "status": status,
        "records_saved": records_collected,
        "error": error,
        "updated_at": now,
    }
    checkpoint.setdefault("retry_counts_by_date", {})[date_key] = attempts
    checkpoint["in_progress_date"] = date_key if status == "running" else None
    checkpoint["last_error"] = error
    checkpoint["last_updated_at"] = now
    if status in DATE_PROVEN_COMPLETE_STATUSES or status == "skipped_resume":
        _append_unique(checkpoint.setdefault("completed_dates", []), date_key)
        checkpoint["last_successful_date"] = date_key
    else:
        completed_dates = checkpoint.setdefault("completed_dates", [])
        if date_key in completed_dates:
            completed_dates.remove(date_key)
    if status == "failed_after_retries":
        _append_unique(checkpoint.setdefault("failed_dates", []), date_key)
    elif status == "blocked_or_access_restricted":
        _append_unique(checkpoint.setdefault("blocked_dates", []), date_key)
    if output_files:
        checkpoint.setdefault("output_files", {}).update({key: value for key, value in output_files.items() if value})
    return checkpoint


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _duration_seconds(started_at: str, completed_at: str) -> float | None:
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(completed_at)
    except ValueError:
        return None
    return round((end - start).total_seconds(), 2)


def update_heartbeat(path: Path, *, job_id: str | None, instance_id: str, current_date: str | None, status: str, last_log_message: str | None) -> Path:
    return atomic_write_json(
        path,
        {
            "job_id": job_id,
            "instance_id": instance_id,
            "current_date": current_date,
            "status": status,
            "last_log_message": last_log_message,
            "last_updated_at": now_text(),
            "process_id": os.getpid(),
        },
    )


def heartbeat_is_stale(path: Path, stale_after_seconds: int = 90) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(str(payload.get("last_updated_at")))
    except Exception:
        return True
    return (datetime.now() - updated).total_seconds() > stale_after_seconds


def proven_completed_dates(
    checkpoint: dict[str, Any],
    database_date_counts: dict[str, int],
) -> set[str]:
    """Return only dates whose checkpoint proves a durable terminal result."""

    date_statuses = checkpoint.get("date_statuses") or {}
    completed: set[str] = set()
    for date_key, row in date_statuses.items():
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        count = int(database_date_counts.get(str(date_key), 0) or 0)
        if status == "completed_results_exhausted":
            completed.add(str(date_key))
        elif status in {"completed_target_reached", "skipped_resume"} and count > 0:
            completed.add(str(date_key))
    return completed


def should_export_snapshot(completed_count: int, last_snapshot_time: float, frequency: str) -> bool:
    if frequency == "every_date":
        return False
    if frequency == "every_5_dates":
        return completed_count > 0 and completed_count % 5 == 0
    if frequency == "every_30_minutes":
        return time.monotonic() - last_snapshot_time >= 30 * 60
    return False


def final_run_status(date_rows: list[dict[str, Any]], stopped: bool, fatal_error: str | None = None) -> str:
    if fatal_error:
        return "fatal_error_with_partial_results"
    if stopped:
        return "stopped_by_user_with_partial_results"
    statuses = {str(row.get("status")) for row in date_rows}
    if "blocked_or_access_restricted" in statuses:
        return "completed_with_blocked_dates"
    if "failed_after_retries" in statuses:
        return "completed_with_failed_dates"
    if "stopped_resource_limit" in statuses:
        return "stopped_resource_limit"
    if statuses and statuses <= {
        "completed_target_reached",
        "completed_results_exhausted",
        "skipped_resume",
    }:
        return "completed_all_dates"
    return "completed_with_failed_dates"
