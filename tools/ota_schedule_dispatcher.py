#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.schedule_dispatcher import dispatch_once
from services.google_drive_sync import (
    drive_configuration_status,
    pending_upload_states,
)
from services.scheduled_instances import SCHEDULED_INSTANCES


def launch_one_pending_drive_retry() -> dict[str, str] | None:
    for instance_id in SCHEDULED_INSTANCES:
        if not pending_upload_states(instance_id):
            continue
        if drive_configuration_status(instance_id)["status"] != "configured":
            continue
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "start",
                f"ota-drive-retry@{instance_id}.service",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return {
            "instance_id": instance_id,
            "status": "started" if result.returncode == 0 else "failed",
            "error": result.stderr.strip() or None,
        }
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dispatch due configurable OTA schedule slots."
    )
    parser.add_argument(
        "--allow-fixed-timer-conflict",
        action="store_true",
        help="Testing only; production dispatch refuses active fixed timers.",
    )
    args = parser.parse_args()
    result = dispatch_once(
        enforce_migration_guard=not args.allow_fixed_timer_conflict
    )
    if result["status"] == "ok":
        result["drive_retry"] = launch_one_pending_drive_retry()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
