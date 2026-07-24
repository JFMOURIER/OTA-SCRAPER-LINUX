from __future__ import annotations

import copy
import fcntl
import json
import os
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from services.job_runner import atomic_write_json
from services.scheduled_instances import (
    INSTANCE_ORDER,
    ScheduledInstanceDefinition,
    get_scheduled_instance,
    resolve_automatic_windows,
)


SCHEDULE_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
TIMEZONE_NAME = "Europe/Paris"
PARIS = ZoneInfo(TIMEZONE_NAME)
DATE_MODE_AUTOMATIC = "automatic"
DATE_MODE_MANUAL = "manual"
DEFAULT_GRACE_MINUTES = 180
DAILY_DEFAULTS = {
    "medium_31_120_days": {
        1: ("00:20",),
        2: ("00:20", "12:20"),
        3: ("00:20", "08:20", "16:20"),
        4: ("00:20", "06:20", "12:20", "18:20"),
    },
    "long_121_365_days": {
        1: ("01:35",),
        2: ("01:35", "13:35"),
    },
}
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ScheduleValidationError(ValueError):
    pass


def paris_now() -> datetime:
    return datetime.now(PARIS)


def schedule_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "config" / "schedule.json"


def schedule_state_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "status" / "schedule_state.json"


def schedule_history_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "status" / "schedule_history.jsonl"


def launch_request_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "status" / "scheduled_launch_request.json"


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=PARIS)
    return value.astimezone(PARIS).isoformat(timespec="seconds")


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=PARIS)
    return parsed.astimezone(PARIS)


def _parse_date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ScheduleValidationError(
            f"{field} must be an ISO date (YYYY-MM-DD)"
        ) from exc


def default_collection_template() -> dict[str, Any]:
    return {
        "source": "Booking.com",
        "city_or_region": "Orlando",
        "nights": 1,
        "adults": 2,
        "rooms": 1,
        "currency": "USD",
        "max_hotels": 250,
        "collect_all_available": False,
        "max_scroll_minutes": 10,
        "selected_star_ratings": [1, 2, 3, 4, 5],
        "include_unknown_star_rating": True,
        "hotels_only": True,
        "performance_mode": "balanced",
        "browser_mode": "headless",
        "retry_failed_dates_automatically": True,
        "max_retries_per_date": 3,
        "continue_if_date_fails": True,
        "auto_export_partial_excel": False,
        "partial_export_frequency": "every_25_dates",
        "local_csv_export": True,
        "local_excel_export": True,
    }


def default_schedule(
    instance_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    definition = get_scheduled_instance(instance_id)
    current = (now or paris_now()).astimezone(PARIS)
    windows = resolve_automatic_windows(current.date())
    start, end = windows[instance_id]
    return {
        "schema_version": SCHEDULE_SCHEMA_VERSION,
        "instance_id": instance_id,
        "enabled": False,
        "timezone": TIMEZONE_NAME,
        "date_mode": DATE_MODE_AUTOMATIC,
        "automatic_window_profile": definition.automatic_window_profile,
        "manual_start_date": start.isoformat(),
        "manual_end_date": end.isoformat(),
        "manual_over_one_year_confirmed": False,
        "frequency_mode": definition.default_frequency_mode,
        "interval_minutes": definition.default_interval_minutes,
        "runs_per_day": definition.default_runs_per_day,
        "daily_run_times": list(definition.default_daily_run_times),
        "collection_template": default_collection_template(),
        "drive_upload_enabled": False,
        "drive_folder_id": definition.drive_folder_id,
        "upload_csv": True,
        "upload_excel": True,
        "grace_period_minutes": DEFAULT_GRACE_MINUTES,
        "schedule_anchor_at": _iso(current.replace(second=0, microsecond=0)),
        "last_modified_at": _iso(current),
        "last_modified_by": "default",
    }


def _validate_collection_template(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScheduleValidationError("collection_template must be an object")
    template = default_collection_template()
    template.update(copy.deepcopy(raw))
    source = str(template.get("source") or "").strip()
    if source not in {"Booking.com", "Demo", "Expedia"}:
        raise ScheduleValidationError("Unsupported collection source")
    template["source"] = source
    template["city_or_region"] = str(
        template.get("city_or_region") or ""
    ).strip()
    if not template["city_or_region"]:
        raise ScheduleValidationError("City or region is required")
    integer_ranges = {
        "nights": (1, 60),
        "adults": (1, 12),
        "rooms": (1, 12),
        "max_hotels": (1, 10000),
        "max_scroll_minutes": (1, 240),
        "max_retries_per_date": (1, 20),
    }
    for field, (minimum, maximum) in integer_ranges.items():
        try:
            value = int(template[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ScheduleValidationError(
                f"collection_template.{field} must be an integer"
            ) from exc
        if value < minimum or value > maximum:
            raise ScheduleValidationError(
                f"collection_template.{field} must be {minimum}–{maximum}"
            )
        template[field] = value
    template["currency"] = str(template.get("currency") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", template["currency"]):
        raise ScheduleValidationError("Currency must be a three-letter code")
    try:
        ratings = sorted(
            {int(value) for value in template["selected_star_ratings"]}
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ScheduleValidationError(
            "selected_star_ratings must contain integers"
        ) from exc
    if not ratings or any(value not in {1, 2, 3, 4, 5} for value in ratings):
        raise ScheduleValidationError("Star ratings must be selected from 1–5")
    template["selected_star_ratings"] = ratings
    for field in (
        "collect_all_available",
        "include_unknown_star_rating",
        "hotels_only",
        "retry_failed_dates_automatically",
        "continue_if_date_fails",
        "auto_export_partial_excel",
        "local_csv_export",
        "local_excel_export",
    ):
        template[field] = bool(template.get(field))
    if template.get("performance_mode") not in {"balanced", "debug"}:
        raise ScheduleValidationError("performance_mode must be balanced or debug")
    if template.get("browser_mode") not in {"headless", "visible"}:
        raise ScheduleValidationError("browser_mode must be headless or visible")
    if template.get("partial_export_frequency") not in {
        "every_25_dates",
        "every_5_dates",
        "every_30_minutes",
    }:
        raise ScheduleValidationError("Unsupported partial export frequency")
    return template


def validate_schedule(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScheduleValidationError("Schedule must be a JSON object")
    instance_id = str(raw.get("instance_id") or "")
    definition = get_scheduled_instance(instance_id)
    base = default_schedule(instance_id)
    payload = copy.deepcopy(base)
    payload.update(copy.deepcopy(raw))
    if int(payload.get("schema_version") or 0) != SCHEDULE_SCHEMA_VERSION:
        raise ScheduleValidationError(
            f"Unsupported schedule schema version: {payload.get('schema_version')}"
        )
    if payload.get("instance_id") != definition.instance_id:
        raise ScheduleValidationError("Schedule instance ID mismatch")
    if payload.get("timezone") != TIMEZONE_NAME:
        raise ScheduleValidationError(
            f"Schedule timezone must be {TIMEZONE_NAME}"
        )
    if payload.get("automatic_window_profile") != definition.instance_id:
        raise ScheduleValidationError("Automatic window profile is invalid")
    payload["enabled"] = bool(payload.get("enabled"))
    date_mode = str(payload.get("date_mode") or "")
    if date_mode not in {DATE_MODE_AUTOMATIC, DATE_MODE_MANUAL}:
        raise ScheduleValidationError("Unsupported date mode")
    payload["date_mode"] = date_mode
    manual_start = _parse_date(
        payload.get("manual_start_date"),
        "manual_start_date",
    )
    manual_end = _parse_date(
        payload.get("manual_end_date"),
        "manual_end_date",
    )
    if manual_start > manual_end:
        raise ScheduleValidationError(
            "Manual start date must not be after manual end date"
        )
    manual_days = (manual_end - manual_start).days + 1
    confirmed = bool(payload.get("manual_over_one_year_confirmed"))
    if date_mode == DATE_MODE_MANUAL and manual_days > 365 and not confirmed:
        raise ScheduleValidationError(
            "Manual ranges over 365 days require explicit confirmation"
        )
    payload["manual_start_date"] = manual_start.isoformat()
    payload["manual_end_date"] = manual_end.isoformat()
    payload["manual_over_one_year_confirmed"] = confirmed
    if definition.instance_id == "near_30_days":
        if payload.get("frequency_mode") != "interval":
            raise ScheduleValidationError("Near schedule must use interval mode")
        try:
            interval = int(payload.get("interval_minutes"))
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError(
                "Near interval must be an integer"
            ) from exc
        if interval < 15 or interval > 1440:
            raise ScheduleValidationError(
                "Near interval must be between 15 and 1440 minutes"
            )
        payload["interval_minutes"] = interval
        payload["runs_per_day"] = None
        payload["daily_run_times"] = []
    else:
        if payload.get("frequency_mode") != "daily":
            raise ScheduleValidationError("This schedule must use daily mode")
        maximum = 4 if definition.instance_id == "medium_31_120_days" else 2
        try:
            runs = int(payload.get("runs_per_day"))
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError(
                "runs_per_day must be an integer"
            ) from exc
        if runs < 1 or runs > maximum:
            raise ScheduleValidationError(
                f"{definition.instance_id} supports 1–{maximum} runs per day"
            )
        raw_times = payload.get("daily_run_times")
        if not isinstance(raw_times, list) or len(raw_times) != runs:
            raise ScheduleValidationError(
                "Daily run time count must match runs_per_day"
            )
        times = [str(value) for value in raw_times]
        if any(not TIME_PATTERN.fullmatch(value) for value in times):
            raise ScheduleValidationError("Daily times must use HH:MM")
        if len(set(times)) != len(times):
            raise ScheduleValidationError("Daily run times must be unique")
        if times != sorted(times):
            raise ScheduleValidationError("Daily run times must be sorted")
        payload["runs_per_day"] = runs
        payload["daily_run_times"] = times
        payload["interval_minutes"] = None
    payload["collection_template"] = _validate_collection_template(
        payload.get("collection_template")
    )
    payload["drive_upload_enabled"] = bool(
        payload.get("drive_upload_enabled")
    )
    payload["drive_folder_id"] = str(
        payload.get("drive_folder_id") or ""
    ).strip()
    payload["upload_csv"] = bool(payload.get("upload_csv"))
    payload["upload_excel"] = bool(payload.get("upload_excel"))
    if payload["drive_upload_enabled"] and not payload["drive_folder_id"]:
        raise ScheduleValidationError(
            "A Google Drive folder ID is required when uploads are enabled"
        )
    if payload["drive_upload_enabled"] and not (
        payload["upload_csv"] or payload["upload_excel"]
    ):
        raise ScheduleValidationError(
            "Enable at least one Drive artifact upload"
        )
    if payload["drive_folder_id"] != definition.drive_folder_id:
        raise ScheduleValidationError(
            "Drive folder ID does not match this isolated instance"
        )
    try:
        grace = int(payload.get("grace_period_minutes"))
    except (TypeError, ValueError) as exc:
        raise ScheduleValidationError(
            "grace_period_minutes must be an integer"
        ) from exc
    if grace < 1 or grace > 1440:
        raise ScheduleValidationError(
            "grace_period_minutes must be between 1 and 1440"
        )
    payload["grace_period_minutes"] = grace
    anchor = _parse_datetime(payload.get("schedule_anchor_at"))
    if anchor is None:
        raise ScheduleValidationError("schedule_anchor_at is invalid")
    payload["schedule_anchor_at"] = _iso(anchor.replace(second=0, microsecond=0))
    resolve_automatic_windows(paris_now().date())
    return payload


def load_schedule(
    instance_id: str,
    *,
    data_dir: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    definition = get_scheduled_instance(instance_id)
    resolved_dir = Path(data_dir or definition.data_dir)
    path = schedule_path(resolved_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return validate_schedule(payload)
    except FileNotFoundError:
        return default_schedule(instance_id, now=now)
    except (OSError, json.JSONDecodeError, ScheduleValidationError) as exc:
        raise ScheduleValidationError(
            f"Could not load valid schedule {path}: {exc}"
        ) from exc


def default_schedule_state(instance_id: str) -> dict[str, Any]:
    get_scheduled_instance(instance_id)
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "instance_id": instance_id,
        "current_schedule_state": "disabled",
        "current_worker_state": "idle",
        "pending_due_run": None,
        "dispatched_slot_keys": [],
        "claimed_slot_keys": [],
        "last_scheduled_run": None,
        "last_completed_run": None,
        "last_successful_run": None,
        "last_failed_run": None,
        "next_scheduled_run": None,
        "last_skip_reason": None,
        "last_defer_reason": None,
        "last_dispatch_at": None,
        "last_worker_pid": None,
        "last_run_id": None,
        "updated_at": None,
    }


def read_schedule_state(
    instance_id: str,
    *,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    definition = get_scheduled_instance(instance_id)
    path = schedule_state_path(data_dir or definition.data_dir)
    state = default_schedule_state(instance_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return state
    if isinstance(payload, dict) and payload.get("instance_id") == instance_id:
        state.update(payload)
    return state


def update_schedule_state(
    instance_id: str,
    updater: Callable[[dict[str, Any]], dict[str, Any] | None],
    *,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    definition = get_scheduled_instance(instance_id)
    resolved_dir = Path(data_dir or definition.data_dir)
    path = schedule_state_path(resolved_dir)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = read_schedule_state(instance_id, data_dir=resolved_dir)
        replacement = updater(copy.deepcopy(state))
        if replacement is not None:
            state = replacement
        state["schema_version"] = STATE_SCHEMA_VERSION
        state["instance_id"] = instance_id
        state["updated_at"] = _iso(paris_now())
        atomic_write_json(path, state)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return state


def record_schedule_event(
    instance_id: str,
    event_type: str,
    *,
    data_dir: str | Path | None = None,
    timestamp: datetime | None = None,
    **details: Any,
) -> dict[str, Any]:
    definition = get_scheduled_instance(instance_id)
    resolved_dir = Path(data_dir or definition.data_dir)
    path = schedule_history_path(resolved_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event_timestamp": _iso(timestamp or paris_now()),
        "instance_id": instance_id,
        "event_type": event_type,
        "schedule_slot": details.pop("schedule_slot", None),
        "resolved_start_date": details.pop("resolved_start_date", None),
        "resolved_end_date": details.pop("resolved_end_date", None),
        "date_mode": details.pop("date_mode", None),
        "frequency_configuration": details.pop(
            "frequency_configuration",
            None,
        ),
        "run_id": details.pop("run_id", None),
        "worker_pid": details.pop("worker_pid", None),
        "host_slot_result": details.pop("host_slot_result", None),
        "skip_or_defer_reason": details.pop("skip_or_defer_reason", None),
        "local_export_status": details.pop("local_export_status", None),
        "drive_upload_status": details.pop("drive_upload_status", None),
        **details,
    }
    line = json.dumps(payload, ensure_ascii=True, default=str)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return payload


def read_schedule_history(
    instance_id: str,
    *,
    data_dir: str | Path | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    definition = get_scheduled_instance(instance_id)
    path = schedule_history_path(data_dir or definition.data_dir)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-max(1, int(limit)) :]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return list(reversed(rows))


def frequency_configuration(schedule: dict[str, Any]) -> dict[str, Any]:
    if schedule["frequency_mode"] == "interval":
        return {
            "mode": "interval",
            "interval_minutes": schedule["interval_minutes"],
        }
    return {
        "mode": "daily",
        "runs_per_day": schedule["runs_per_day"],
        "daily_run_times": list(schedule["daily_run_times"]),
    }


def frequency_description(schedule: dict[str, Any]) -> str:
    if schedule["frequency_mode"] == "interval":
        minutes = int(schedule["interval_minutes"])
        hours, remainder = divmod(minutes, 60)
        readable = []
        if hours:
            readable.append(f"{hours}h")
        if remainder or not readable:
            readable.append(f"{remainder}m")
        return f"Every {minutes} minutes ({' '.join(readable)})"
    return (
        f"{schedule['runs_per_day']} run(s) per day at "
        + ", ".join(schedule["daily_run_times"])
    )


def resolved_dates_for_schedule(
    schedule: dict[str, Any],
    anchor_date: date,
) -> tuple[date, date]:
    schedule = validate_schedule(schedule)
    if schedule["date_mode"] == DATE_MODE_MANUAL:
        return (
            _parse_date(schedule["manual_start_date"], "manual_start_date"),
            _parse_date(schedule["manual_end_date"], "manual_end_date"),
        )
    return resolve_automatic_windows(anchor_date)[schedule["instance_id"]]


def _daily_slot_datetimes(
    schedule: dict[str, Any],
    day: date,
) -> list[datetime]:
    rows = []
    for value in schedule["daily_run_times"]:
        parsed = time.fromisoformat(value)
        rows.append(
            datetime.combine(day, parsed, tzinfo=PARIS)
        )
    return rows


def current_due_slot(
    schedule: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    schedule = validate_schedule(schedule)
    current = (now or paris_now()).astimezone(PARIS)
    if not schedule["enabled"]:
        return None
    pending = state.get("pending_due_run")
    if isinstance(pending, dict) and pending.get("slot_key"):
        return dict(pending)
    processed = set(state.get("dispatched_slot_keys") or [])
    if schedule["frequency_mode"] == "interval":
        anchor = _parse_datetime(schedule["schedule_anchor_at"])
        if anchor is None or current < anchor:
            return None
        interval = timedelta(minutes=int(schedule["interval_minutes"]))
        anchor_utc = anchor.astimezone(UTC)
        current_utc = current.astimezone(UTC)
        boundaries = int(
            (current_utc - anchor_utc).total_seconds()
            // interval.total_seconds()
        )
        scheduled_for = (
            anchor_utc + boundaries * interval
        ).astimezone(PARIS)
        if scheduled_for <= anchor and boundaries == 0:
            return None
        slot_key = (
            f"{schedule['instance_id']}:interval:"
            f"{scheduled_for.isoformat(timespec='minutes')}"
        )
    else:
        candidates = [
            value
            for value in _daily_slot_datetimes(schedule, current.date())
            if value <= current
        ]
        if not candidates:
            return None
        scheduled_for = candidates[-1]
        slot_key = (
            f"{schedule['instance_id']}:daily:"
            f"{scheduled_for.date().isoformat()}:{scheduled_for.strftime('%H:%M')}"
        )
    if slot_key in processed:
        return None
    start, end = resolved_dates_for_schedule(schedule, current.date())
    return {
        "slot_key": slot_key,
        "scheduled_for": _iso(scheduled_for),
        "first_due_at": _iso(current),
        "resolved_start_date": start.isoformat(),
        "resolved_end_date": end.isoformat(),
        "date_mode": schedule["date_mode"],
        "frequency_configuration": frequency_configuration(schedule),
    }


def next_scheduled_run(
    schedule: dict[str, Any],
    state: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    del state
    schedule = validate_schedule(schedule)
    if not schedule["enabled"]:
        return None
    current = (now or paris_now()).astimezone(PARIS)
    if schedule["frequency_mode"] == "interval":
        anchor = _parse_datetime(schedule["schedule_anchor_at"])
        if anchor is None:
            return None
        if current < anchor:
            return anchor
        interval = timedelta(minutes=int(schedule["interval_minutes"]))
        anchor_utc = anchor.astimezone(UTC)
        current_utc = current.astimezone(UTC)
        count = int(
            (current_utc - anchor_utc).total_seconds()
            // interval.total_seconds()
        ) + 1
        return (anchor_utc + count * interval).astimezone(PARIS)
    for candidate in _daily_slot_datetimes(schedule, current.date()):
        if candidate > current:
            return candidate
    return _daily_slot_datetimes(
        schedule,
        current.date() + timedelta(days=1),
    )[0]


def save_schedule(
    instance_id: str,
    raw: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    modified_by: str = "streamlit",
    now: datetime | None = None,
) -> dict[str, Any]:
    definition = get_scheduled_instance(instance_id)
    resolved_dir = Path(data_dir or definition.data_dir)
    current = (now or paris_now()).astimezone(PARIS)
    prior = load_schedule(instance_id, data_dir=resolved_dir, now=current)
    candidate = copy.deepcopy(raw)
    candidate["instance_id"] = instance_id
    candidate["schema_version"] = SCHEDULE_SCHEMA_VERSION
    candidate["timezone"] = TIMEZONE_NAME
    candidate["automatic_window_profile"] = instance_id
    candidate["last_modified_at"] = _iso(current)
    candidate["last_modified_by"] = modified_by
    if bool(candidate.get("enabled")) and (
        not prior.get("enabled")
        or prior.get("frequency_mode") != candidate.get("frequency_mode")
        or prior.get("interval_minutes") != candidate.get("interval_minutes")
        or prior.get("daily_run_times") != candidate.get("daily_run_times")
    ):
        candidate["schedule_anchor_at"] = _iso(
            current.replace(second=0, microsecond=0)
        )
    validated = validate_schedule(candidate)
    atomic_write_json(schedule_path(resolved_dir), validated)
    event_common = {
        "date_mode": validated["date_mode"],
        "frequency_configuration": frequency_configuration(validated),
    }
    start, end = resolved_dates_for_schedule(validated, current.date())
    event_common.update(
        resolved_start_date=start.isoformat(),
        resolved_end_date=end.isoformat(),
    )
    record_schedule_event(
        instance_id,
        "schedule_saved",
        data_dir=resolved_dir,
        **event_common,
    )
    if bool(prior.get("enabled")) != validated["enabled"]:
        record_schedule_event(
            instance_id,
            "schedule_enabled" if validated["enabled"] else "schedule_disabled",
            data_dir=resolved_dir,
            **event_common,
        )
    next_run = next_scheduled_run(validated, now=current)

    def state_update(state: dict[str, Any]) -> dict[str, Any]:
        state["current_schedule_state"] = (
            "enabled" if validated["enabled"] else "disabled"
        )
        state["next_scheduled_run"] = _iso(next_run) if next_run else None
        if not validated["enabled"]:
            state["pending_due_run"] = None
        return state

    update_schedule_state(instance_id, state_update, data_dir=resolved_dir)
    return validated


def disable_schedule(
    instance_id: str,
    *,
    data_dir: str | Path | None = None,
    modified_by: str = "streamlit",
) -> dict[str, Any]:
    schedule = load_schedule(instance_id, data_dir=data_dir)
    schedule["enabled"] = False
    return save_schedule(
        instance_id,
        schedule,
        data_dir=data_dir,
        modified_by=modified_by,
    )


def ensure_default_schedule_file(
    instance_id: str,
    *,
    data_dir: str | Path | None = None,
) -> Path:
    definition = get_scheduled_instance(instance_id)
    resolved_dir = Path(data_dir or definition.data_dir)
    path = schedule_path(resolved_dir)
    if not path.exists():
        atomic_write_json(path, default_schedule(instance_id))
    return path


def build_launch_request(
    schedule: dict[str, Any],
    slot: dict[str, Any],
    *,
    request_id: str | None = None,
    trigger: str = "scheduled",
) -> dict[str, Any]:
    validated = validate_schedule(schedule)
    template = copy.deepcopy(validated["collection_template"])
    return {
        "schema_version": 1,
        "request_id": request_id or str(uuid4()),
        "instance_id": validated["instance_id"],
        "trigger": trigger,
        "schedule_slot": slot["slot_key"],
        "scheduled_for": slot["scheduled_for"],
        "requested_at": _iso(paris_now()),
        "resolved_start_date": slot["resolved_start_date"],
        "resolved_end_date": slot["resolved_end_date"],
        "date_mode": slot["date_mode"],
        "frequency_configuration": slot["frequency_configuration"],
        "collection_template": template,
        "drive_upload_enabled": validated["drive_upload_enabled"],
        "drive_folder_id": validated["drive_folder_id"],
        "upload_csv": validated["upload_csv"],
        "upload_excel": validated["upload_excel"],
    }


def claim_launch_request(
    instance_id: str,
    *,
    data_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    definition = get_scheduled_instance(instance_id)
    resolved_dir = Path(data_dir or definition.data_dir)
    path = launch_request_path(resolved_dir)
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(request, dict) or request.get("instance_id") != instance_id:
        return None
    slot_key = str(request.get("schedule_slot") or "")
    if not slot_key:
        return None
    claimed = False

    def claim(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal claimed
        keys = list(state.get("claimed_slot_keys") or [])
        if slot_key in keys:
            return state
        keys.append(slot_key)
        state["claimed_slot_keys"] = keys[-1000:]
        state["current_worker_state"] = "starting"
        state["last_scheduled_run"] = request.get("scheduled_for")
        state["last_worker_pid"] = os.getpid()
        claimed = True
        return state

    update_schedule_state(instance_id, claim, data_dir=resolved_dir)
    return request if claimed else None


def mark_slot_dispatched(
    instance_id: str,
    slot: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    slot_key = str(slot["slot_key"])

    def update(state: dict[str, Any]) -> dict[str, Any]:
        keys = list(state.get("dispatched_slot_keys") or [])
        if slot_key not in keys:
            keys.append(slot_key)
        state["dispatched_slot_keys"] = keys[-1000:]
        state["pending_due_run"] = None
        state["last_dispatch_at"] = _iso(paris_now())
        return state

    return update_schedule_state(instance_id, update, data_dir=data_dir)


def mark_worker_finished(
    instance_id: str,
    *,
    status: str,
    run_id: int | None,
    worker_pid: int,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    now_text = _iso(paris_now())
    successful = status in {"completed", "completed_all_dates"}

    def update(state: dict[str, Any]) -> dict[str, Any]:
        state["current_worker_state"] = "idle"
        state["last_completed_run"] = now_text
        state["last_worker_pid"] = worker_pid
        state["last_run_id"] = run_id
        if successful:
            state["last_successful_run"] = now_text
        elif status and not status.startswith("scheduled_run_skipped"):
            state["last_failed_run"] = now_text
        return state

    return update_schedule_state(instance_id, update, data_dir=data_dir)


def default_daily_times(instance_id: str, runs_per_day: int) -> tuple[str, ...]:
    try:
        return DAILY_DEFAULTS[instance_id][int(runs_per_day)]
    except (KeyError, ValueError) as exc:
        raise ScheduleValidationError(
            f"No daily defaults for {instance_id} at {runs_per_day} run(s)"
        ) from exc


def instance_paths_are_isolated() -> bool:
    roots = [get_scheduled_instance(value).data_dir for value in INSTANCE_ORDER]
    return len(roots) == len(set(path.resolve() for path in roots))
