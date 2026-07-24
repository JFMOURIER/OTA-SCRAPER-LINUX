from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from services.job_runner import atomic_write_json
from services.scheduled_instances import SCHEDULED_INSTANCES


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_STATE_DIR = PROJECT_ROOT / "data" / "scheduler"
CONCURRENCY_ENABLE_MARKER = (
    SCHEDULER_STATE_DIR / "concurrency_3_enabled.json"
)
MINIMUM_USABLE_RAM_GB = 28.0
DEFAULT_MINIMUM_DISK_FREE_GB = 20.0


def _port_available(port: int) -> tuple[bool, str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", int(port)))
    except OSError as exc:
        return False, str(exc)
    return True, "available"


def prepare_instance_directories() -> list[str]:
    created: list[str] = []
    for definition in SCHEDULED_INSTANCES.values():
        for path in (
            definition.data_dir,
            definition.data_dir / "exports",
            definition.data_dir / "partial",
            definition.data_dir / "status",
            definition.data_dir / "status" / "drive_uploads",
            definition.data_dir / "config",
            definition.data_dir / "logs",
            definition.data_dir / "debug",
            definition.data_dir / "checkpoints",
            definition.data_dir / "browser_profile" / "runs",
        ):
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path.resolve()))
    SCHEDULER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return created


def hardware_preflight(
    *,
    minimum_ram_gb: float = MINIMUM_USABLE_RAM_GB,
    minimum_disk_free_gb: float | None = None,
    require_ports_available: bool = True,
) -> dict[str, Any]:
    disk_minimum = (
        float(minimum_disk_free_gb)
        if minimum_disk_free_gb is not None
        else float(
            os.getenv(
                "OTA_PREFLIGHT_MIN_DISK_GB",
                str(DEFAULT_MINIMUM_DISK_FREE_GB),
            )
        )
    )
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage(PROJECT_ROOT)
    total_ram_gb = memory.total / (1024**3)
    disk_free_gb = disk.free / (1024**3)
    checks: list[dict[str, Any]] = [
        {
            "check": "usable_ram_at_least_28_gb",
            "ok": total_ram_gb >= minimum_ram_gb,
            "actual": round(total_ram_gb, 2),
            "required": minimum_ram_gb,
        },
        {
            "check": "swap_available",
            "ok": swap.total > 0,
            "actual": round(swap.total / (1024**3), 2),
            "required": "> 0 GB",
        },
        {
            "check": "sufficient_disk_space",
            "ok": disk_free_gb >= disk_minimum,
            "actual": round(disk_free_gb, 2),
            "required": disk_minimum,
        },
    ]
    for definition in SCHEDULED_INSTANCES.values():
        checks.append(
            {
                "check": f"instance_directory_{definition.instance_id}",
                "ok": definition.data_dir.is_dir(),
                "actual": str(definition.data_dir.resolve()),
                "required": "directory exists",
            }
        )
    for definition in SCHEDULED_INSTANCES.values():
        available, detail = _port_available(definition.port)
        checks.append(
            {
                "check": f"port_{definition.port}_available",
                "ok": available if require_ports_available else True,
                "actual": detail,
                "required": (
                    "available"
                    if require_ports_available
                    else "not enforced for this check"
                ),
            }
        )
    failed = [check for check in checks if not check["ok"]]
    return {
        "status": "passed" if not failed else "failed",
        "checked_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "checks": checks,
        "failed_checks": [check["check"] for check in failed],
        "requested_max_concurrent_workers": 3,
    }


def enable_three_worker_concurrency() -> dict[str, Any]:
    report = hardware_preflight(require_ports_available=True)
    if report["status"] != "passed":
        raise RuntimeError(
            "32 GB concurrency preflight failed: "
            + ", ".join(report["failed_checks"])
        )
    SCHEDULER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        **report,
        "enabled": True,
        "enabled_at": datetime.now().isoformat(
            sep=" ",
            timespec="seconds",
        ),
    }
    atomic_write_json(CONCURRENCY_ENABLE_MARKER, payload)
    return payload


def three_worker_concurrency_enabled() -> bool:
    try:
        payload = json.loads(
            CONCURRENCY_ENABLE_MARKER.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or not payload.get("enabled"):
        return False
    if payload.get("status") != "passed":
        return False
    live = hardware_preflight(require_ports_available=False)
    directory_checks = [
        check
        for check in live["checks"]
        if check["check"].startswith("instance_directory_")
    ]
    critical_checks = live["checks"][:3] + directory_checks
    return all(check["ok"] for check in critical_checks)
