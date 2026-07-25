#!/usr/bin/env python3
"""Bounded, PID-verified escalation for one scheduled scraper worker."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.stop_control import finalize_stopped_run


def log(message: str) -> None:
    print(
        f"[{datetime.now().isoformat(sep=' ', timespec='seconds')}] "
        f"{message}",
        flush=True,
    )


def still_exact_worker(
    pid: int,
    *,
    create_time: float,
    instance_id: str,
) -> bool:
    try:
        process = psutil.Process(pid)
        command = " ".join(process.cmdline())
        return (
            abs(process.create_time() - create_time) < 0.01
            and "tools/ota_scheduled_run.py" in command
            and instance_id in command
        )
    except (psutil.Error, OSError):
        return False


def cancellation_still_requested(data_dir: Path, job_id: str) -> bool:
    try:
        payload = json.loads(
            (data_dir / "status" / "cancel_request.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    requested_job = str(payload.get("job_id") or "")
    return bool(payload.get("cancel_requested")) and (
        not job_id or not requested_job or requested_job == job_id
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--create-time", type=float, required=True)
    parser.add_argument("--job-id", default="")
    parser.add_argument("--grace-seconds", type=float, default=45.0)
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()

    deadline = time.monotonic() + max(5.0, args.grace_seconds)
    while time.monotonic() < deadline:
        if not cancellation_still_requested(data_dir, args.job_id):
            log("Cancellation was cleared by a deliberate new run; exiting.")
            return 0
        if not still_exact_worker(
            args.pid,
            create_time=args.create_time,
            instance_id=args.instance,
        ):
            log("Scheduled worker exited cooperatively.")
            finalize_stopped_run(args.instance, data_dir=data_dir)
            return 0
        time.sleep(0.5)

    if not cancellation_still_requested(data_dir, args.job_id):
        return 0
    if not still_exact_worker(
        args.pid,
        create_time=args.create_time,
        instance_id=args.instance,
    ):
        finalize_stopped_run(args.instance, data_dir=data_dir)
        return 0
    os.kill(args.pid, signal.SIGTERM)
    log(f"SIGTERM sent only to scheduled scraper PID {args.pid}.")
    for _ in range(20):
        if not still_exact_worker(
            args.pid,
            create_time=args.create_time,
            instance_id=args.instance,
        ):
            finalize_stopped_run(args.instance, data_dir=data_dir)
            log("Stopped run finalized after SIGTERM.")
            return 0
        time.sleep(0.5)
    log("Worker is still exiting after SIGTERM; no SIGKILL was used.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
