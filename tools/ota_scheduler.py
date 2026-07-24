#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.hardware_preflight import (
    enable_three_worker_concurrency,
    hardware_preflight,
    prepare_instance_directories,
)
from services.scheduled_instances import SCHEDULED_INSTANCES


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare and validate isolated OTA scheduled instances."
    )
    parser.add_argument(
        "command",
        choices={"definitions", "prepare", "preflight", "enable-concurrency"},
    )
    args = parser.parse_args()
    if args.command == "definitions":
        payload = {
            key: {
                "port": value.port,
                "data_dir": str(value.data_dir),
                "display": value.display,
                "start_offset_days": value.start_offset_days,
                "end_offset_days": value.end_offset_days,
                "timer_schedule": value.timer_schedule,
            }
            for key, value in SCHEDULED_INSTANCES.items()
        }
    elif args.command == "prepare":
        payload = {"created_or_verified": prepare_instance_directories()}
    elif args.command == "preflight":
        payload = hardware_preflight()
    else:
        payload = enable_three_worker_concurrency()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("status", "passed") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
