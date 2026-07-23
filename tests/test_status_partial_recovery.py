from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import openpyxl

import app
import database.db as db
from services.exporter import (
    export_sqlite_run_to_csv,
    export_sqlite_run_to_excel,
)
from services.job_runner import (
    atomic_write_json,
    build_checkpoint,
    final_run_status,
    save_partial_records,
    update_checkpoint_date,
)
from services.partial_recovery import (
    inspect_partial_pair,
    recover_checkpoint_status_failures,
    recover_partial_date,
)
from services.status_reporting import build_status_fields


DUPLICATE_STATUS_ERROR = (
    "__mp_main__.update_status_file() got multiple values for keyword argument "
    "'available_ram_mb'"
)


def partial_rows(stay_date: date) -> list[dict]:
    checkout_date = stay_date.replace(day=stay_date.day + 1)
    return [
        {
            "collection_run_id": 999,
            "source": "Booking.com Playwright slow validation mode",
            "city_or_region": "Orlando",
            "hotel_name": f"Hotel {index}",
            "hotel_url": f"https://www.booking.com/hotel/us/hotel-{index}.html",
            "raw_price_text": f"US${100 + index}",
            "parsed_price": str(100 + index),
            "cheapest_price_total": f"US${100 + index}",
            "currency": "USD",
            "checkin_date": stay_date.isoformat(),
            "checkout_date": checkout_date.isoformat(),
            "number_of_nights": 1,
            "adults": 2,
            "collection_status": "success",
            "collected_at": datetime(2026, 7, 23, 12, 0).isoformat(sep=" "),
        }
        for index in (1, 2)
    ]


class StatusPayloadTests(unittest.TestCase):
    def test_authoritative_available_ram_is_passed_once_and_metrics_survive(self):
        resource_metrics = {
            "available_ram_mb": 800.0,
            "swap_used_mb": 2048.0,
            "swap_free_mb": 2048.0,
            "swap_percent": 50.0,
            "python_rss_mb": 150.0,
            "browser_rss_mb": 900.0,
            "browser_process_count": 8,
            "disk_free_gb": 120.0,
            "resource_guard_level": "ok",
        }
        fields = build_status_fields(
            status_updates={
                "current_message": "partial saved",
                "available_ram_mb": 700.0,
            },
            resource_metrics=resource_metrics,
            authoritative_fields={"available_ram_mb": 1600.0},
        )
        status_writer = Mock()

        status_writer(**fields)

        received = status_writer.call_args.kwargs
        self.assertEqual(received["available_ram_mb"], 1600.0)
        self.assertEqual(
            [key for key in received if key == "available_ram_mb"],
            ["available_ram_mb"],
        )
        for key, value in resource_metrics.items():
            if key != "available_ram_mb":
                self.assertEqual(received[key], value)

    def test_nonessential_status_write_failure_is_logged_and_not_raised(self):
        stderr = io.StringIO()
        with patch.object(app, "read_current_status_file", return_value={}), patch.object(
            app, "write_json_file", side_effect=OSError("read-only status directory")
        ), redirect_stderr(stderr):
            succeeded = app.update_status_file(
                status="completed_all_dates",
                available_ram_mb=1600.0,
            )

        self.assertFalse(succeeded)
        self.assertIn("nonessential status-file update failed", stderr.getvalue())
        self.assertIn("read-only status directory", stderr.getvalue())


class PartialRecoveryTests(unittest.TestCase):
    def test_partial_resume_finalizes_idempotently_and_exports_both_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "test.sqlite"
            partial_dir = root / "partial"
            first_date = date(2026, 8, 1)
            second_date = date(2026, 8, 2)
            dates = [first_date, second_date]

            for stay_date in dates:
                save_partial_records(partial_dir, stay_date, partial_rows(stay_date))
                inspection = inspect_partial_pair(partial_dir, stay_date)
                self.assertEqual(inspection.json_record_count, 2)
                self.assertEqual(inspection.csv_record_count, 2)
                self.assertTrue(inspection.required_core_fields_present)
                self.assertEqual(inspection.records_with_hotel_name, 2)
                self.assertEqual(inspection.records_with_raw_price, 2)
                self.assertEqual(inspection.records_with_parsed_price, 2)
                self.assertEqual(inspection.malformed_records, 0)

            with patch.object(db, "SQLITE_DB_PATH", database_path):
                db.init_db("sqlite")
                run_id = db.create_collection_run(
                    "Booking.com",
                    "Orlando",
                    first_date,
                    date(2026, 8, 3),
                    1,
                    2,
                    "USD",
                    250,
                    backend="sqlite",
                )
                checkpoint = build_checkpoint(
                    job_id="job-test",
                    run_id=run_id,
                    source="Booking.com",
                    city="Orlando",
                    start_date=first_date,
                    end_date=second_date,
                    nights=1,
                    planned_dates=dates,
                    signature={"test": True},
                )
                for stay_date in dates:
                    update_checkpoint_date(
                        checkpoint,
                        stay_date=stay_date,
                        checkout_date=stay_date.replace(day=stay_date.day + 1),
                        status="failed_after_retries",
                        attempts=3,
                        records_collected=0,
                        error=DUPLICATE_STATUS_ERROR,
                    )
                checkpoint_path = root / "checkpoint.json"
                atomic_write_json(checkpoint_path, checkpoint)

                recovered = recover_checkpoint_status_failures(
                    checkpoint,
                    planned_dates=dates,
                    partial_dir=partial_dir,
                    run_id=run_id,
                    source="Booking.com",
                    city_or_region="Orlando",
                    number_of_nights=1,
                    adults=2,
                    currency="USD",
                    backend="sqlite",
                )

                self.assertEqual([row.recovered_records for row in recovered], [2, 2])
                self.assertEqual(db.count_results_by_run_id(run_id, "sqlite"), 4)
                self.assertEqual(
                    db.result_date_counts_by_run_id(run_id, "sqlite"),
                    {"2026-08-01": 2, "2026-08-02": 2},
                )
                self.assertEqual(checkpoint["failed_dates"], [])
                self.assertTrue(
                    all(
                        row["status"] == "completed"
                        and row["recovered_from_partial"]
                        for row in checkpoint["date_statuses"].values()
                    )
                )
                self.assertEqual(
                    final_run_status(
                        list(checkpoint["date_statuses"].values()),
                        stopped=False,
                    ),
                    "completed_all_dates",
                )

                second_resume = recover_checkpoint_status_failures(
                    checkpoint,
                    planned_dates=dates,
                    partial_dir=partial_dir,
                    run_id=run_id,
                    source="Booking.com",
                    city_or_region="Orlando",
                    number_of_nights=1,
                    adults=2,
                    currency="USD",
                    backend="sqlite",
                )
                self.assertEqual(
                    [row.database_rows_after for row in second_resume],
                    [2, 2],
                )
                self.assertEqual(db.count_results_by_run_id(run_id, "sqlite"), 4)

                for stay_date in dates:
                    second_pass = recover_partial_date(
                        partial_dir,
                        stay_date=stay_date,
                        run_id=run_id,
                        source="Booking.com",
                        city_or_region="Orlando",
                        number_of_nights=1,
                        adults=2,
                        currency="USD",
                        backend="sqlite",
                    )
                    self.assertEqual(second_pass.database_rows_after, 2)
                self.assertEqual(db.count_results_by_run_id(run_id, "sqlite"), 4)

                summary = {
                    "source": "Booking.com",
                    "city_or_region": "Orlando",
                    "checkin_date": first_date,
                    "number_of_nights": 1,
                    "__date_status_rows": list(
                        checkpoint["date_statuses"].values()
                    ),
                }
                csv_path = export_sqlite_run_to_csv(
                    run_id,
                    summary,
                    root,
                    "final.csv",
                    instance_id="test",
                    batch_size=1,
                )
                excel_path = export_sqlite_run_to_excel(
                    run_id,
                    summary,
                    root,
                    "final.xlsx",
                    batch_size=1,
                )
                with csv_path.open(encoding="utf-8", newline="") as handle:
                    csv_rows = list(csv.DictReader(handle))
                self.assertEqual(len(csv_rows), 4)
                self.assertEqual(
                    {row["checkin_date"] for row in csv_rows},
                    {"2026-08-01", "2026-08-02"},
                )
                workbook = openpyxl.load_workbook(
                    excel_path, read_only=True, data_only=True
                )
                try:
                    self.assertEqual(workbook["All Results"].max_row - 1, 4)
                finally:
                    workbook.close()

                with patch.object(
                    app, "read_current_status_file", return_value={}
                ), patch.object(
                    app,
                    "write_json_file",
                    side_effect=OSError("telemetry unavailable"),
                ):
                    self.assertFalse(
                        app.update_status_file(status="completed_all_dates")
                    )
                self.assertEqual(db.count_results_by_run_id(run_id, "sqlite"), 4)
                self.assertNotIn("failed_after_retries", {
                    row["status"] for row in checkpoint["date_statuses"].values()
                })


if __name__ == "__main__":
    unittest.main()
