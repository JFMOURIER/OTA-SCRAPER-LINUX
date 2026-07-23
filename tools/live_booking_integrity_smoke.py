#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import os
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psutil


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one isolated visible Booking browser-to-export smoke."
    )
    parser.add_argument(
        "--mode",
        choices=("fresh", "interrupt", "resume", "boundary"),
        required=True,
    )
    parser.add_argument("--checkin", required=True)
    parser.add_argument("--max-hotels", type=int, default=250)
    parser.add_argument("--interrupt-after", type=int, default=75)
    return parser.parse_args()


class SmokeQueue:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.job_id: str | None = None
        self.instance_log_file: str | None = None

    def put(self, item: Any) -> None:
        try:
            event, payload = item
        except (TypeError, ValueError):
            event, payload = "unknown", item
        if event not in {"log", "complete", "failed", "run_id", "records_saved"}:
            return
        line = json.dumps(
            {
                "at": datetime.now().isoformat(),
                "event": event,
                "payload": payload,
            },
            default=str,
            ensure_ascii=True,
        )
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def build_config(checkin: date, *, resume: bool, maximum: int):
    from app import CollectionConfig

    return CollectionConfig(
        source="Booking.com",
        city_or_region="Orlando",
        checkin_start=checkin,
        checkin_end=checkin,
        nights=1,
        adults=2,
        currency="USD",
        max_hotels=maximum,
        headless=False,
        collect_all_available=False,
        max_scroll_minutes=5,
        selected_star_ratings=(1, 2, 3, 4, 5),
        include_unknown_star_rating=True,
        debug_mode=False,
        screenshots_enabled=False,
        fast_mode=False,
        performance_mode="balanced",
        block_images_and_fonts=False,
        test_mode=False,
        db_backend="sqlite",
        hotels_only=True,
        disable_filters_during_complete_collection=False,
        ultra_reliable_loading_mode=False,
        resume_previous_run=resume,
        retry_failed_dates_automatically=False,
        max_retries_per_date=1,
        continue_if_date_fails=False,
        auto_export_partial_excel=False,
    )


def invoke_runner(
    config,
    stop_event,
    queue: SmokeQueue,
    job_id: str,
    log_path: Path,
) -> None:
    from app import (
        run_background_job_with_fatal_guard,
        run_resilient_collection_job,
    )

    run_background_job_with_fatal_guard(
        run_resilient_collection_job,
        config,
        stop_event,
        queue,
        job_id,
        str(log_path),
    )


def partial_record_count(data_dir: Path) -> tuple[int, Path | None]:
    candidates = sorted(
        (data_dir / "partial").glob(
            "runs/run_*/*_partial_hotels.json"
        )
    )
    for path in reversed(candidates):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(rows, list):
            return len(rows), path
    return 0, None


def remaining_profile_processes(data_dir: Path) -> list[dict[str, Any]]:
    marker = str((data_dir / "browser_profile").resolve())
    matches: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "cmdline", "name"]):
        try:
            command = " ".join(process.info.get("cmdline") or [])
            if marker in command:
                matches.append(
                    {
                        "pid": process.pid,
                        "name": process.info.get("name"),
                        "command": command,
                    }
                )
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return matches


def newest(paths) -> Path | None:
    values = [path for path in paths if path.is_file()]
    return max(values, key=lambda path: path.stat().st_mtime) if values else None


def csv_rows(path: Path | None) -> int:
    if path is None:
        return 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def excel_rows(path: Path | None) -> int:
    if path is None:
        return 0
    import pandas as pd

    return len(pd.read_excel(path, sheet_name="All Results"))


def final_report(
    data_dir: Path,
    *,
    mode: str,
    requested_checkin: date,
    maximum: int,
) -> dict[str, Any]:
    database = data_dir / "hotel_price_collector.sqlite"
    run: dict[str, Any] = {}
    rows = 0
    duplicates = 0
    suspicious = 0
    date_values: list[tuple[Any, ...]] = []
    if database.exists():
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            latest = connection.execute(
                "select * from collection_runs order by id desc limit 1"
            ).fetchone()
            run = dict(latest) if latest is not None else {}
            run_id = run.get("id")
            if run_id is not None:
                rows = int(
                    connection.execute(
                        "select count(*) from hotel_price_results "
                        "where collection_run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                )
                unique = int(
                    connection.execute(
                        """
                        select count(*) from (
                          select checkin_date,
                                 lower(coalesce(nullif(hotel_url, ''), hotel_name))
                          from hotel_price_results
                          where collection_run_id = ?
                          group by checkin_date,
                                   lower(coalesce(nullif(hotel_url, ''), hotel_name))
                        )
                        """,
                        (run_id,),
                    ).fetchone()[0]
                )
                duplicates = rows - unique
                suspicious = int(
                    connection.execute(
                        """
                        select count(*) from hotel_price_results
                        where collection_run_id = ?
                          and lower(coalesce(hotel_name, '')) glob
                              '*private room*'
                           or collection_run_id = ?
                          and lower(coalesce(hotel_name, '')) glob
                              '*vacation home*'
                        """,
                        (run_id, run_id),
                    ).fetchone()[0]
                )
                date_values = [
                    tuple(value)
                    for value in connection.execute(
                        """
                        select distinct
                            requested_checkin_date,
                            requested_checkout_date,
                            effective_checkin_date,
                            effective_checkout_date,
                            date_integrity_verified
                        from hotel_price_results
                        where collection_run_id = ?
                        """,
                        (run_id,),
                    ).fetchall()
                ]

    status_path = data_dir / "status" / "current_job_status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        status = {}
    checkpoint_path = data_dir / "checkpoints" / "current_run_resume.json"
    try:
        checkpoint = json.loads(
            checkpoint_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        checkpoint = {}
    date_row = (
        (checkpoint.get("date_statuses") or {}).get(
            requested_checkin.isoformat()
        )
        or {}
    )
    csv_path = newest((data_dir / "exports").glob("*.csv"))
    excel_path = newest((data_dir / "exports").glob("*.xlsx"))
    partial_count, partial_path = partial_record_count(data_dir)
    profile_processes = remaining_profile_processes(data_dir)
    report = {
        "mode": mode,
        "instance_id": os.getenv("INSTANCE_ID"),
        "data_dir": str(data_dir.resolve()),
        "requested_checkin": requested_checkin.isoformat(),
        "requested_checkout": date.fromordinal(
            requested_checkin.toordinal() + 1
        ).isoformat(),
        "maximum_hotels": maximum,
        "run": run,
        "sqlite_path": str(database.resolve()),
        "sqlite_rows": rows,
        "duplicate_rows": duplicates,
        "date_integrity_values": date_values,
        "date_status": date_row,
        "status": status,
        "csv_path": str(csv_path.resolve()) if csv_path else None,
        "csv_rows": csv_rows(csv_path),
        "excel_path": str(excel_path.resolve()) if excel_path else None,
        "excel_rows": excel_rows(excel_path),
        "partial_path": str(partial_path.resolve()) if partial_path else None,
        "partial_rows": partial_count,
        "suspicious_private_or_vacation_names": suspicious,
        "remaining_profile_browser_processes": profile_processes,
    }
    if mode == "interrupt":
        report["passed"] = (
            partial_count > 0 and not profile_processes
        )
    else:
        expected_dates = (
            requested_checkin.isoformat(),
            date.fromordinal(requested_checkin.toordinal() + 1).isoformat(),
        )
        dates_verified = date_values == [
            (*expected_dates, *expected_dates, 1)
        ]
        report["passed"] = bool(
            run.get("status") == "completed_all_dates"
            and rows > 0
            and rows <= maximum
            and rows == report["csv_rows"] == report["excel_rows"]
            and duplicates == 0
            and date_row.get("status")
            in {
                "completed_target_reached",
                "completed_verified_end_of_results",
                "completed_max_scroll_time_with_results",
                "completed_verified_plateau",
            }
            and dates_verified
            and status.get("last_error") is None
            and not profile_processes
        )
    return report


def main() -> int:
    args = arguments()
    checkin = date.fromisoformat(args.checkin)
    if checkin <= date.today():
        raise SystemExit("--checkin must be in the future")
    data_dir = Path(os.environ["INSTANCE_DATA_DIR"]).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "logs" / f"live_{args.mode}.jsonl"
    config = build_config(
        checkin,
        resume=args.mode == "resume",
        maximum=args.max_hotels,
    )
    job_id = f"{os.getenv('INSTANCE_ID')}-{args.mode}-{int(time.time())}"

    if args.mode == "interrupt":
        context = multiprocessing.get_context("fork")
        stop_event = context.Event()
        queue = SmokeQueue(log_path)
        process = context.Process(
            target=invoke_runner,
            args=(config, stop_event, queue, job_id, log_path),
        )
        process.start()
        deadline = time.monotonic() + 360
        observed = 0
        while process.is_alive() and time.monotonic() < deadline:
            observed, _ = partial_record_count(data_dir)
            if observed >= max(1, args.interrupt_after):
                stop_event.set()
                break
            time.sleep(0.2)
        process.join(timeout=90)
        if process.is_alive():
            stop_event.set()
            process.join(timeout=30)
        if process.is_alive():
            raise SystemExit(
                "Interrupted smoke did not stop cleanly within 120 seconds."
            )
    else:
        from threading import Event

        invoke_runner(
            config,
            Event(),
            SmokeQueue(log_path),
            job_id,
            log_path,
        )

    report = final_report(
        data_dir,
        mode=args.mode,
        requested_checkin=checkin,
        maximum=args.max_hotels,
    )
    report_path = data_dir / "live_smoke_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
