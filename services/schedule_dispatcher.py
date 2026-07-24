from __future__ import annotations

import fcntl
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from services.job_runner import atomic_write_json
from services.resource_guard import (
    ConcurrencyUpgradeNotReady,
    HostConcurrencyLimitReached,
    HostWorkerSemaphore,
)
from services.schedule_config import (
    PARIS,
    build_launch_request,
    current_due_slot,
    frequency_configuration,
    launch_request_path,
    load_schedule,
    mark_slot_dispatched,
    next_scheduled_run,
    paris_now,
    read_schedule_state,
    record_schedule_event,
    resolved_dates_for_schedule,
    update_schedule_state,
)
from services.scheduled_instances import (
    SCHEDULED_INSTANCES,
    ScheduledInstanceDefinition,
    get_scheduled_instance,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST_STATE_DIR = PROJECT_ROOT / "data" / "scheduler"
OLD_FIXED_TIMERS = (
    "ota-scraper-near.timer",
    "ota-scraper-medium.timer",
    "ota-scraper-long.timer",
)
Launcher = Callable[[str, dict[str, Any]], bool]


def active_old_fixed_timers() -> list[str]:
    active: list[str] = []
    for unit in OLD_FIXED_TIMERS:
        for user_scope in (False, True):
            command = ["systemctl"]
            if user_scope:
                command.append("--user")
            command.extend(["is-active", "--quiet", unit])
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                active.append(unit)
                break
    return active


def launch_systemd_worker(
    instance_id: str,
    _request: dict[str, Any],
) -> bool:
    get_scheduled_instance(instance_id)
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "start",
            f"ota-scraper-run@{instance_id}.service",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"Could not start scheduler worker for {instance_id}"
        )
    return True


def _advisory_lock_busy(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def host_capacity_available(
    state_dir: str | Path = HOST_STATE_DIR,
) -> bool:
    semaphore: HostWorkerSemaphore | None = None
    try:
        semaphore = HostWorkerSemaphore(state_dir)
        semaphore.acquire(instance_id="dispatcher-probe", job_id=str(uuid4()))
    except HostConcurrencyLimitReached:
        return False
    except ConcurrencyUpgradeNotReady:
        return False
    finally:
        if semaphore is not None:
            semaphore.release()
    return True


def _set_next_run(
    definition: ScheduledInstanceDefinition,
    schedule: dict[str, Any],
    now: datetime,
) -> None:
    next_run = next_scheduled_run(schedule, now=now)

    def update(state: dict[str, Any]) -> dict[str, Any]:
        state["next_scheduled_run"] = (
            next_run.isoformat(timespec="seconds") if next_run else None
        )
        state["current_schedule_state"] = (
            "enabled" if schedule["enabled"] else "disabled"
        )
        state["last_dispatch_at"] = now.isoformat(timespec="seconds")
        return state

    update_schedule_state(
        definition.instance_id,
        update,
        data_dir=definition.data_dir,
    )


def _persist_pending(
    definition: ScheduledInstanceDefinition,
    slot: dict[str, Any],
) -> None:
    def update(state: dict[str, Any]) -> dict[str, Any]:
        if not state.get("pending_due_run"):
            state["pending_due_run"] = slot
        return state

    update_schedule_state(
        definition.instance_id,
        update,
        data_dir=definition.data_dir,
    )


def _set_reason(
    definition: ScheduledInstanceDefinition,
    *,
    skip: str | None = None,
    defer: str | None = None,
) -> None:
    def update(state: dict[str, Any]) -> dict[str, Any]:
        if skip is not None:
            state["last_skip_reason"] = skip
        if defer is not None:
            state["last_defer_reason"] = defer
        return state

    update_schedule_state(
        definition.instance_id,
        update,
        data_dir=definition.data_dir,
    )


def dispatch_instance(
    definition: ScheduledInstanceDefinition,
    *,
    now: datetime | None = None,
    launcher: Launcher | None = None,
    capacity_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    current = (now or paris_now()).astimezone(PARIS)
    launcher = launcher or launch_systemd_worker
    capacity_check = capacity_check or host_capacity_available
    schedule = load_schedule(
        definition.instance_id,
        data_dir=definition.data_dir,
        now=current,
    )
    state = read_schedule_state(
        definition.instance_id,
        data_dir=definition.data_dir,
    )
    _set_next_run(definition, schedule, current)
    if not schedule["enabled"]:
        return {
            "instance_id": definition.instance_id,
            "action": "disabled",
        }
    slot = current_due_slot(schedule, state, now=current)
    if slot is None:
        return {
            "instance_id": definition.instance_id,
            "action": "not_due",
        }
    if not state.get("pending_due_run"):
        _persist_pending(definition, slot)
        record_schedule_event(
            definition.instance_id,
            "scheduled_run_due",
            data_dir=definition.data_dir,
            schedule_slot=slot["slot_key"],
            resolved_start_date=slot["resolved_start_date"],
            resolved_end_date=slot["resolved_end_date"],
            date_mode=slot["date_mode"],
            frequency_configuration=slot["frequency_configuration"],
        )
    scheduled_for = datetime.fromisoformat(slot["scheduled_for"]).astimezone(PARIS)
    grace = timedelta(minutes=int(schedule["grace_period_minutes"]))
    if current - scheduled_for > grace:
        reason = "scheduled_run_grace_period_expired"
        _set_reason(definition, skip=reason)
        mark_slot_dispatched(
            definition.instance_id,
            slot,
            data_dir=definition.data_dir,
        )
        record_schedule_event(
            definition.instance_id,
            reason,
            data_dir=definition.data_dir,
            schedule_slot=slot["slot_key"],
            resolved_start_date=slot["resolved_start_date"],
            resolved_end_date=slot["resolved_end_date"],
            date_mode=slot["date_mode"],
            frequency_configuration=slot["frequency_configuration"],
            skip_or_defer_reason=reason,
        )
        return {
            "instance_id": definition.instance_id,
            "action": "grace_expired",
            "slot_key": slot["slot_key"],
        }
    if _advisory_lock_busy(
        definition.data_dir / "status" / "active_scraper.lock"
    ) or _advisory_lock_busy(
        definition.data_dir / "status" / "scheduled_run.lock"
    ):
        reason = "scheduled_run_skipped_previous_run_active"
        _set_reason(definition, skip=reason)
        mark_slot_dispatched(
            definition.instance_id,
            slot,
            data_dir=definition.data_dir,
        )
        record_schedule_event(
            definition.instance_id,
            reason,
            data_dir=definition.data_dir,
            schedule_slot=slot["slot_key"],
            resolved_start_date=slot["resolved_start_date"],
            resolved_end_date=slot["resolved_end_date"],
            date_mode=slot["date_mode"],
            frequency_configuration=slot["frequency_configuration"],
            skip_or_defer_reason=reason,
        )
        return {
            "instance_id": definition.instance_id,
            "action": "skipped_active",
            "slot_key": slot["slot_key"],
        }
    if not capacity_check():
        reason = "scheduled_run_deferred_host_capacity"
        _set_reason(definition, defer=reason)
        record_schedule_event(
            definition.instance_id,
            reason,
            data_dir=definition.data_dir,
            schedule_slot=slot["slot_key"],
            resolved_start_date=slot["resolved_start_date"],
            resolved_end_date=slot["resolved_end_date"],
            date_mode=slot["date_mode"],
            frequency_configuration=slot["frequency_configuration"],
            host_slot_result="unavailable",
            skip_or_defer_reason=reason,
        )
        return {
            "instance_id": definition.instance_id,
            "action": "deferred_host_capacity",
            "slot_key": slot["slot_key"],
        }
    request = build_launch_request(schedule, slot)
    atomic_write_json(launch_request_path(definition.data_dir), request)
    try:
        launched = bool(launcher(definition.instance_id, request))
    except Exception as exc:
        _set_reason(definition, defer=str(exc))
        record_schedule_event(
            definition.instance_id,
            "scheduled_run_deferred_host_capacity",
            data_dir=definition.data_dir,
            schedule_slot=slot["slot_key"],
            resolved_start_date=slot["resolved_start_date"],
            resolved_end_date=slot["resolved_end_date"],
            date_mode=slot["date_mode"],
            frequency_configuration=slot["frequency_configuration"],
            host_slot_result="launcher_error",
            skip_or_defer_reason=str(exc),
        )
        return {
            "instance_id": definition.instance_id,
            "action": "launcher_error",
            "error": str(exc),
            "slot_key": slot["slot_key"],
        }
    if not launched:
        return {
            "instance_id": definition.instance_id,
            "action": "launcher_declined",
            "slot_key": slot["slot_key"],
        }
    mark_slot_dispatched(
        definition.instance_id,
        slot,
        data_dir=definition.data_dir,
    )
    return {
        "instance_id": definition.instance_id,
        "action": "launched",
        "slot_key": slot["slot_key"],
        "request_path": str(
            launch_request_path(definition.data_dir).resolve()
        ),
    }


def dispatch_once(
    *,
    now: datetime | None = None,
    definitions: Iterable[ScheduledInstanceDefinition] | None = None,
    launcher: Launcher | None = None,
    capacity_check: Callable[[], bool] | None = None,
    enforce_migration_guard: bool = True,
) -> dict[str, Any]:
    current = (now or paris_now()).astimezone(PARIS)
    if enforce_migration_guard:
        active = active_old_fixed_timers()
        if active:
            return {
                "status": "migration_conflict",
                "active_old_fixed_timers": active,
                "dispatched_at": current.isoformat(timespec="seconds"),
                "instances": [],
            }
    selected = list(definitions or SCHEDULED_INSTANCES.values())
    rows = [
        dispatch_instance(
            definition,
            now=current,
            launcher=launcher,
            capacity_check=capacity_check,
        )
        for definition in selected
    ]
    return {
        "status": "ok",
        "dispatched_at": current.isoformat(timespec="seconds"),
        "instances": rows,
    }


def request_run_once(
    instance_id: str,
    *,
    data_dir: str | Path | None = None,
    now: datetime | None = None,
    launcher: Launcher | None = None,
    capacity_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    definition = get_scheduled_instance(instance_id)
    resolved_dir = Path(data_dir or definition.data_dir)
    current = (now or paris_now()).astimezone(PARIS)
    launcher = launcher or launch_systemd_worker
    capacity_check = capacity_check or host_capacity_available
    schedule = load_schedule(instance_id, data_dir=resolved_dir, now=current)
    start, end = resolved_dates_for_schedule(schedule, current.date())
    slot = {
        "slot_key": (
            f"{instance_id}:manual:{current.isoformat(timespec='seconds')}:"
            f"{uuid4()}"
        ),
        "scheduled_for": current.isoformat(timespec="seconds"),
        "first_due_at": current.isoformat(timespec="seconds"),
        "resolved_start_date": start.isoformat(),
        "resolved_end_date": end.isoformat(),
        "date_mode": schedule["date_mode"],
        "frequency_configuration": frequency_configuration(schedule),
    }
    if _advisory_lock_busy(
        resolved_dir / "status" / "active_scraper.lock"
    ) or _advisory_lock_busy(
        resolved_dir / "status" / "scheduled_run.lock"
    ):
        reason = "scheduled_run_skipped_previous_run_active"
        record_schedule_event(
            instance_id,
            reason,
            data_dir=resolved_dir,
            schedule_slot=slot["slot_key"],
            resolved_start_date=start.isoformat(),
            resolved_end_date=end.isoformat(),
            date_mode=schedule["date_mode"],
            frequency_configuration=slot["frequency_configuration"],
            skip_or_defer_reason=reason,
        )
        return {"status": "not_started", "reason": reason}
    if not capacity_check():
        reason = "scheduled_run_deferred_host_capacity"
        record_schedule_event(
            instance_id,
            reason,
            data_dir=resolved_dir,
            schedule_slot=slot["slot_key"],
            resolved_start_date=start.isoformat(),
            resolved_end_date=end.isoformat(),
            date_mode=schedule["date_mode"],
            frequency_configuration=slot["frequency_configuration"],
            host_slot_result="unavailable",
            skip_or_defer_reason=reason,
        )
        return {"status": "not_started", "reason": reason}
    request = build_launch_request(
        schedule,
        slot,
        trigger="manual_run_once",
    )
    atomic_write_json(launch_request_path(resolved_dir), request)
    if not launcher(instance_id, request):
        return {"status": "not_started", "reason": "launcher_declined"}
    mark_slot_dispatched(instance_id, slot, data_dir=resolved_dir)
    return {
        "status": "started",
        "schedule_slot": slot["slot_key"],
        "resolved_start_date": start.isoformat(),
        "resolved_end_date": end.isoformat(),
    }


def read_launch_request(
    instance_id: str,
    *,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    definition = get_scheduled_instance(instance_id)
    path = launch_request_path(data_dir or definition.data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}
