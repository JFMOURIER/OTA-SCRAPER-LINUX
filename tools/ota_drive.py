#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.drive_delivery import (
    drive_configuration_status,
    pending_upload_states,
    retry_latest_failed_upload,
    test_drive_folder_access,
)
from services.scheduled_instances import SCHEDULED_INSTANCES


def _instances(value: str | None) -> list[str]:
    if value:
        if value not in SCHEDULED_INSTANCES:
            raise ValueError(f"Unknown instance: {value}")
        return [value]
    return list(SCHEDULED_INSTANCES)


def main() -> int:
    parser = argparse.ArgumentParser(description="OTA Google Drive delivery")
    parser.add_argument(
        "command",
        choices={"status", "test", "retry-pending"},
    )
    parser.add_argument("--instance")
    args = parser.parse_args()
    payload: dict[str, object] = {}
    exit_code = 0
    for instance_id in _instances(args.instance):
        if args.command == "status":
            result = drive_configuration_status(instance_id)
            result["pending_uploads"] = len(
                pending_upload_states(instance_id)
            )
        elif args.command == "test":
            result = test_drive_folder_access(instance_id)
        else:
            result = retry_latest_failed_upload(instance_id)
        payload[instance_id] = result
        if result.get("status") == "not_configured":
            exit_code = max(exit_code, 2)
        if result.get("drive_upload_status") in {
            "failed",
            "partially_succeeded",
            "not_configured",
        }:
            exit_code = max(exit_code, 1)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
