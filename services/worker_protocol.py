from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.job_runner import atomic_write_json, now_text


VALID_ACTIONS = {"start", "stop", "resume", "clear_completed"}


@dataclass(frozen=True, slots=True)
class WorkerPaths:
    status_dir: Path

    @property
    def request(self) -> Path:
        return self.status_dir / "worker_request.json"

    @property
    def status(self) -> Path:
        return self.status_dir / "worker_status.json"

    @property
    def heartbeat(self) -> Path:
        return self.status_dir / "heartbeat.json"

    @property
    def cancel(self) -> Path:
        return self.status_dir / "cancel_request.json"

    @property
    def active_job(self) -> Path:
        return self.status_dir / "active_job.json"

    @property
    def restart_history(self) -> Path:
        return self.status_dir / "worker_restart_history.json"

    @property
    def supervisor_pid(self) -> Path:
        return self.status_dir / "worker_supervisor.pid"

    @property
    def child_pid(self) -> Path:
        return self.status_dir / "worker_child.pid"

    @property
    def lock(self) -> Path:
        return self.status_dir / "worker_supervisor.lock"


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_status(paths: WorkerPaths, **updates: Any) -> dict[str, Any]:
    payload = read_json(paths.status)
    payload.update(updates)
    payload["updated_at"] = now_text()
    atomic_write_json(paths.status, payload)
    return payload


def validate_request(payload: dict[str, Any], instance_id: str) -> dict[str, Any]:
    request_id = str(payload.get("request_id") or "").strip()
    action = str(payload.get("action") or "").strip().lower()
    requested_instance = str(payload.get("instance_id") or "").strip()
    if not request_id:
        raise ValueError("worker request is missing request_id")
    if action not in VALID_ACTIONS:
        raise ValueError(f"unsupported worker action: {action or '<empty>'}")
    if requested_instance != instance_id:
        raise ValueError(
            f"worker request instance mismatch: expected {instance_id}, received {requested_instance or '<empty>'}"
        )
    if action in {"start", "resume"}:
        config = payload.get("job_config")
        if not isinstance(config, dict):
            raise ValueError(f"{action} request is missing complete job_config")
        required = {"source", "city_or_region", "checkin_start", "checkin_end", "max_hotels"}
        missing = sorted(required - set(config))
        if missing:
            raise ValueError(f"job_config is missing: {', '.join(missing)}")
    return dict(payload)


def job_is_terminal(status: str | None) -> bool:
    return str(status or "") in {
        "completed",
        "completed_all_dates",
        "completed_with_failed_dates",
        "completed_with_blocked_dates",
        "stopped_by_user",
        "stopped_by_user_with_partial_results",
        "fatal_config_error",
        "failed_restart_limit",
    }
