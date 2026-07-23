#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely finalize telemetry-failed partial hotel records."
    )
    parser.add_argument(
        "--instance-data-dir",
        required=True,
        type=Path,
        help="Instance data directory containing SQLite, partial, and checkpoint data.",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help="Checkpoint to recover (defaults to checkpoints/current_run_resume.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report recoverable records without writing anything.",
    )
    return parser.parse_args()


def planned_dates(start: date, end: date) -> list[date]:
    return [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    ]


def csv_row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def excel_row_count(path: Path) -> int:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return max(0, workbook["All Results"].max_row - 1)
    finally:
        workbook.close()


def main() -> int:
    args = parse_args()
    instance_dir = args.instance_data_dir.resolve()
    checkpoint_path = (
        args.checkpoint.resolve()
        if args.checkpoint
        else instance_dir / "checkpoints" / "current_run_resume.json"
    )
    if not instance_dir.is_dir():
        raise SystemExit(f"Instance data directory not found: {instance_dir}")
    if not checkpoint_path.is_file():
        raise SystemExit(f"Checkpoint not found: {checkpoint_path}")

    os.environ["INSTANCE_ID"] = instance_dir.name
    os.environ["INSTANCE_DATA_DIR"] = str(instance_dir)
    os.environ["DB_BACKEND"] = "sqlite"

    from database import db
    from services.exporter import (
        create_summary,
        export_sqlite_run_to_csv,
        export_sqlite_run_to_excel,
    )
    from services.job_runner import atomic_write_json, final_run_status
    from services.partial_recovery import (
        inspect_partial_pair,
        recover_checkpoint_status_failures,
    )
    from services.resource_guard import SingleScraperLock

    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if not isinstance(checkpoint, dict):
        raise SystemExit(f"Checkpoint is not a JSON object: {checkpoint_path}")
    run_id = int(checkpoint.get("run_id") or 0)
    if run_id <= 0:
        raise SystemExit("Checkpoint has no valid run_id")

    run = next(
        (
            row
            for row in db.fetch_collection_runs(limit=1000, backend="sqlite")
            if int(row["id"]) == run_id
        ),
        None,
    )
    if run is None:
        raise SystemExit(f"SQLite collection run {run_id} was not found")

    start_date = date.fromisoformat(str(checkpoint["start_date"]))
    end_date = date.fromisoformat(str(checkpoint["end_date"]))
    dates = planned_dates(start_date, end_date)
    signature = checkpoint.get("signature") or {}
    source = str(checkpoint.get("source") or run["source"])
    city = str(checkpoint.get("city") or run["city_or_region"])
    nights = int(checkpoint.get("length_of_stay") or run["number_of_nights"])
    adults = int(signature.get("adults") or run["adults"])
    currency = str(signature.get("currency") or run["currency"])

    if str(run["city_or_region"]).strip().lower() != city.strip().lower():
        raise SystemExit("Checkpoint city does not match the SQLite run")
    if str(run["checkin_date"]) != start_date.isoformat():
        raise SystemExit("Checkpoint start date does not match the SQLite run")

    print(f"Recovery mode: {'DRY RUN' if args.dry_run else 'WRITE'}")
    print(f"Instance data: {instance_dir}")
    print(f"Checkpoint: {checkpoint_path}")
    print(f"SQLite run: {run_id}")
    for stay_date in dates:
        inspection = inspect_partial_pair(instance_dir / "partial", stay_date)
        print(
            json.dumps(
                {
                    "stay_date": inspection.stay_date,
                    "partial_record_count": inspection.json_record_count,
                    "csv_record_count": inspection.csv_record_count,
                    "required_core_fields_present": inspection.required_core_fields_present,
                    "records_with_hotel_name": inspection.records_with_hotel_name,
                    "records_with_raw_price": inspection.records_with_raw_price,
                    "records_with_parsed_price": inspection.records_with_parsed_price,
                    "malformed_records": inspection.malformed_records,
                },
                sort_keys=True,
            )
        )

    lock_path = instance_dir / "status" / "active_scraper.lock"
    lock = None
    read_only_lock = None
    if args.dry_run:
        if lock_path.exists():
            read_only_lock = lock_path.open("r", encoding="utf-8")
            try:
                fcntl.flock(
                    read_only_lock.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                read_only_lock.close()
                raise SystemExit(
                    f"Another scraper owns {lock_path}; dry run refused."
                ) from exc
    else:
        lock = SingleScraperLock(lock_path)
        lock.acquire(f"partial-recovery-run-{run_id}")
    try:
        recovery_results = recover_checkpoint_status_failures(
            checkpoint,
            planned_dates=dates,
            partial_dir=instance_dir / "partial",
            run_id=run_id,
            source=source,
            city_or_region=city,
            number_of_nights=nights,
            adults=adults,
            currency=currency,
            backend="sqlite",
            dry_run=args.dry_run,
            log=print,
        )
        if len(recovery_results) != len(dates):
            raise SystemExit(
                "Not every requested date is recoverable from the known "
                "duplicate resource-status failure."
            )
        if args.dry_run:
            print(
                f"Dry run complete: {sum(row.recovered_records for row in recovery_results)} "
                "records validated; no files or database rows changed."
            )
            return 0

        date_rows = list((checkpoint.get("date_statuses") or {}).values())
        date_rows.sort(key=lambda row: str(row.get("checkin_date") or ""))
        status = final_run_status(date_rows, stopped=False, fatal_error=None)
        if status != "completed_all_dates":
            raise RuntimeError(f"Recovered checkpoint did not finalize cleanly: {status}")

        all_results = db.fetch_results_by_run_id(run_id, backend="sqlite")
        summary: dict[str, Any] = create_summary(
            all_results,
            {
                "source": source,
                "city_or_region": city,
                "checkin_date": start_date,
                "checkout_date": end_date + timedelta(days=nights),
                "number_of_nights": nights,
                "adults": adults,
                "currency": currency,
                "started_at": run.get("started_at"),
                "completed_at": datetime.now(),
                "completion status": status,
                "__date_status_rows": date_rows,
            },
        )
        summary["run_id"] = run_id
        summary["instance_id"] = instance_dir.name
        summary["__date_status_rows"] = date_rows

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_dir = instance_dir / "exports"
        excel_path = export_sqlite_run_to_excel(
            run_id,
            summary,
            export_dir,
            filename=f"partial_recovery_run_{run_id}_{stamp}.xlsx",
        )
        csv_path = export_sqlite_run_to_csv(
            run_id,
            summary,
            export_dir,
            filename=f"partial_recovery_run_{run_id}_{stamp}.csv",
            instance_id=instance_dir.name,
        )
        db.update_collection_run_excel_path(
            run_id, str(excel_path), backend="sqlite"
        )
        db.update_collection_run_status(
            run_id, status, None, backend="sqlite"
        )

        checkpoint["status"] = status
        checkpoint["last_error"] = None
        checkpoint.setdefault("output_files", {})["final_excel"] = str(excel_path)
        checkpoint.setdefault("output_files", {})["final_csv"] = str(csv_path)
        checkpoint.setdefault("output_files", {})["final_csv_status"] = "succeeded"
        atomic_write_json(checkpoint_path, checkpoint)
        current_checkpoint = instance_dir / "checkpoints" / "current_run_resume.json"
        if current_checkpoint.resolve() != checkpoint_path:
            atomic_write_json(current_checkpoint, checkpoint)

        database_rows = db.count_results_by_run_id(run_id, backend="sqlite")
        csv_rows = csv_row_count(csv_path)
        excel_rows = excel_row_count(excel_path)
        status_path = instance_dir / "status" / "current_job_status.json"
        try:
            current_status = (
                json.loads(status_path.read_text(encoding="utf-8"))
                if status_path.exists()
                else {}
            )
            if not isinstance(current_status, dict):
                current_status = {}
            current_status.update(
                {
                    "status": status,
                    "current_message": (
                        f"Recovered {database_rows} rows from protected partial files"
                    ),
                    "last_error": None,
                    "hotels_collected_total": database_rows,
                    "csv_file_path": str(csv_path),
                    "csv_export_status": "succeeded",
                    "csv_rows_exported": csv_rows,
                    "latest_excel_file": str(excel_path),
                    "last_updated_at": datetime.now().isoformat(
                        sep=" ", timespec="seconds"
                    ),
                }
            )
            atomic_write_json(status_path, current_status)
        except Exception as exc:
            print(
                "Warning: recovery data was finalized, but the nonessential "
                f"status file could not be updated: {exc}",
                file=sys.stderr,
            )

        recovery_log = instance_dir / "logs" / f"partial_recovery_run_{run_id}_{stamp}.json"
        atomic_write_json(
            recovery_log,
            {
                "run_id": run_id,
                "status": status,
                "database_rows": database_rows,
                "date_rows": {
                    row.inspection.stay_date: row.database_rows_after
                    for row in recovery_results
                },
                "csv_path": str(csv_path),
                "csv_rows": csv_rows,
                "excel_path": str(excel_path),
                "excel_rows": excel_rows,
                "completed_at": datetime.now().isoformat(
                    sep=" ", timespec="seconds"
                ),
            },
        )
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": status,
                    "database_rows": database_rows,
                    "csv_path": str(csv_path),
                    "csv_rows": csv_rows,
                    "excel_path": str(excel_path),
                    "excel_rows": excel_rows,
                    "recovery_log": str(recovery_log.resolve()),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        if lock is not None:
            lock.release()
        if read_only_lock is not None:
            fcntl.flock(read_only_lock.fileno(), fcntl.LOCK_UN)
            read_only_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
