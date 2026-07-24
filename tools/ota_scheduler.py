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
from services.schedule_config import (
    disable_schedule,
    ensure_default_schedule_file,
    load_schedule,
    next_scheduled_run,
    read_schedule_state,
)
from services.schedule_dispatcher import active_old_fixed_timers


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare and validate isolated OTA scheduled instances."
    )
    parser.add_argument(
        "command",
        choices={
            "definitions",
            "prepare",
            "preflight",
            "enable-concurrency",
            "initialize-schedules",
            "disable-all-schedules",
            "schedule-status",
            "migration-status",
        },
    )
    args = parser.parse_args()
    if args.command == "definitions":
        payload = {
            key: {
                "port": value.port,
                "data_dir": str(value.data_dir),
                "display": value.display,
                "drive_folder_id": value.drive_folder_id,
                "default_frequency_mode": value.default_frequency_mode,
                "default_interval_minutes": value.default_interval_minutes,
                "default_runs_per_day": value.default_runs_per_day,
                "default_daily_run_times": value.default_daily_run_times,
            }
            for key, value in SCHEDULED_INSTANCES.items()
        }
    elif args.command == "prepare":
        payload = {"created_or_verified": prepare_instance_directories()}
    elif args.command == "preflight":
        payload = hardware_preflight()
    elif args.command == "enable-concurrency":
        payload = enable_three_worker_concurrency()
    elif args.command == "initialize-schedules":
        payload = {
            "status": "passed",
            "schedule_files": [
                str(
                    ensure_default_schedule_file(
                        instance_id,
                        data_dir=definition.data_dir,
                    ).resolve()
                )
                for instance_id, definition in SCHEDULED_INSTANCES.items()
            ],
        }
    elif args.command == "disable-all-schedules":
        payload = {
            "status": "passed",
            "disabled": [
                disable_schedule(
                    instance_id,
                    data_dir=definition.data_dir,
                    modified_by="ota-scheduler-cli",
                )["instance_id"]
                for instance_id, definition in SCHEDULED_INSTANCES.items()
            ],
        }
    elif args.command == "schedule-status":
        payload = {
            "status": "passed",
            "instances": {
                instance_id: {
                    "schedule": (
                        schedule := load_schedule(
                            instance_id,
                            data_dir=definition.data_dir,
                        )
                    ),
                    "state": read_schedule_state(
                        instance_id,
                        data_dir=definition.data_dir,
                    ),
                    "next_scheduled_run": (
                        str(next_scheduled_run(schedule))
                        if schedule["enabled"]
                        else None
                    ),
                }
                for instance_id, definition in SCHEDULED_INSTANCES.items()
            },
        }
    else:
        active = active_old_fixed_timers()
        payload = {
            "status": "passed" if not active else "failed",
            "active_old_fixed_timers": active,
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("status", "passed") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
