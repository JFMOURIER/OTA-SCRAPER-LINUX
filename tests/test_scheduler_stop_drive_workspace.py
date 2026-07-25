from __future__ import annotations

import csv
import json
import math
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import database.db as db
from services.drive_delivery import upload_run_bundle
from services.job_runner import atomic_write_json
from services.operational_status import REQUIRED_OPERATIONAL_FIELDS
from services.run_exports import automatic_export_run_csv
from services.schedule_config import (
    DATE_MODE_AUTOMATIC,
    PARIS,
    default_schedule,
    interval_seconds_for_runs_per_day,
    load_schedule,
    resolved_dates_for_schedule,
    save_schedule,
)
from services.scheduled_instances import SCHEDULED_INSTANCES
from services.stop_control import finalize_stopped_run, request_instance_stop
from services.workspace_control import (
    resolve_workspace_index,
    select_owned_window_ids,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeStopEvent:
    def __init__(self) -> None:
        self.sources: list[str] = []

    def set(self, source: str) -> None:
        self.sources.append(source)


def result_row(run_id: int, index: int) -> dict:
    return {
        "collection_run_id": run_id,
        "source": "Demo",
        "city_or_region": "Orlando",
        "hotel_name": f"Hotel {index}",
        "ota_hotel_id": str(index),
        "hotel_url": f"https://example.test/{index}",
        "raw_price_text": f"US$ {100 + index}",
        "parsed_price": 100 + index,
        "cheapest_price_total": 100 + index,
        "currency": "USD",
        "checkin_date": date(2026, 7, 25),
        "checkout_date": date(2026, 7, 26),
        "requested_checkin_date": date(2026, 7, 25),
        "requested_checkout_date": date(2026, 7, 26),
        "effective_checkin_date": date(2026, 7, 25),
        "effective_checkout_date": date(2026, 7, 26),
        "date_integrity_verified": True,
        "number_of_nights": 1,
        "adults": 2,
        "collection_status": "success",
        "collected_at": datetime(2026, 7, 25, 12, 0),
    }


def create_running_run(database_path: Path, rows: int) -> int:
    with patch.object(db, "SQLITE_DB_PATH", database_path):
        db.init_db("sqlite")
        run_id = db.create_collection_run(
            "Demo",
            "Orlando",
            date(2026, 7, 25),
            date(2026, 7, 26),
            1,
            2,
            "USD",
            rows,
            backend="sqlite",
        )
        db.insert_hotel_results(
            [result_row(run_id, index) for index in range(rows)],
            backend="sqlite",
        )
    return run_id


class SchedulerContractTests(unittest.TestCase):
    def test_all_scraper_1_runs_per_day_values_have_exact_interval(self):
        for runs in range(1, 25):
            with self.subTest(runs=runs):
                self.assertTrue(
                    math.isclose(
                        interval_seconds_for_runs_per_day(runs),
                        86_400 / runs,
                        rel_tol=0,
                        abs_tol=1e-9,
                    )
                )

    def test_paris_cycle_dates_are_today_through_exactly_plus_30(self):
        schedule = default_schedule(
            "near_30_days",
            now=datetime(2026, 7, 25, 0, 5, tzinfo=PARIS),
        )
        schedule["date_mode"] = DATE_MODE_AUTOMATIC
        self.assertEqual(
            resolved_dates_for_schedule(schedule, date(2026, 7, 25)),
            (date(2026, 7, 25), date(2026, 8, 24)),
        )
        self.assertEqual(
            resolved_dates_for_schedule(schedule, date(2026, 7, 26)),
            (date(2026, 7, 26), date(2026, 8, 25)),
        )

    def test_runs_per_day_persists_across_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule = default_schedule("near_30_days")
            schedule["enabled"] = True
            schedule["runs_per_day"] = 7
            save_schedule(
                "near_30_days",
                schedule,
                data_dir=directory,
            )
            reloaded = load_schedule(
                "near_30_days",
                data_dir=directory,
            )
            self.assertEqual(reloaded["runs_per_day"], 7)
            self.assertAlmostEqual(
                reloaded["interval_seconds"],
                86_400 / 7,
            )


class StopContractTests(unittest.TestCase):
    def test_top_and_sidebar_are_wired_to_the_same_backend(self):
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        sidebar_source = (
            ROOT / "services" / "schedule_ui.py"
        ).read_text(encoding="utf-8")
        self.assertIn('_request_stop_from_ui("top")', app_source)
        self.assertIn(
            "stop_callback=_request_stop_from_ui",
            app_source,
        )
        self.assertIn('stop_callback("sidebar")', sidebar_source)
        self.assertEqual(
            app_source.count("def _request_stop_from_ui("),
            1,
        )

    def test_stop_disables_only_its_scheduler_and_persists_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            near_dir = root / "near"
            medium_dir = root / "medium"
            for instance_id, data_dir in (
                ("near_30_days", near_dir),
                ("medium_31_120_days", medium_dir),
            ):
                schedule = default_schedule(instance_id)
                schedule["enabled"] = True
                save_schedule(instance_id, schedule, data_dir=data_dir)
            atomic_write_json(
                near_dir / "status" / "current_job_status.json",
                {"job_id": "near-job", "status": "running"},
            )
            event = FakeStopEvent()
            result = request_instance_stop(
                "near_30_days",
                data_dir=near_dir,
                source="top",
                in_process_job={
                    "id": "near-job",
                    "stop_event": event,
                },
            )
            self.assertEqual(result["status"], "stop_requested")
            self.assertEqual(event.sources, ["top"])
            self.assertFalse(
                load_schedule(
                    "near_30_days",
                    data_dir=near_dir,
                )["enabled"]
            )
            self.assertTrue(
                load_schedule(
                    "medium_31_120_days",
                    data_dir=medium_dir,
                )["enabled"]
            )
            cancel = json.loads(
                (
                    near_dir / "status" / "cancel_request.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(cancel["stop_source"], "top")
            self.assertTrue(cancel["stop_requested"])

    def test_stopped_run_exports_only_explicit_run_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "near"
            database_path = data_dir / "hotel_price_collector.sqlite"
            data_dir.mkdir(parents=True)
            first = create_running_run(database_path, 2)
            create_running_run(database_path, 3)
            schedule = default_schedule("near_30_days")
            save_schedule("near_30_days", schedule, data_dir=data_dir)
            atomic_write_json(
                data_dir / "status" / "current_job_status.json",
                {
                    "job_id": "stopped-job",
                    "status": "stopping",
                    "current_run_id": first,
                    "stop_requested": True,
                },
            )
            with patch.dict(
                "os.environ",
                {"OTA_DOWNLOADS_DIR": str(root / "Downloads")},
            ):
                finalized = finalize_stopped_run(
                    "near_30_days",
                    data_dir=data_dir,
                    run_id=first,
                )
            export = finalized["export"]
            self.assertEqual(finalized["status"], "stopped_by_user")
            self.assertEqual(export["csv_rows_exported"], 2)
            self.assertIn("_partial_", Path(export["csv_file_path"]).name)
            with Path(export["csv_file_path"]).open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as handle:
                self.assertEqual(sum(1 for _ in csv.DictReader(handle)), 2)
            self.assertIn(
                "OTA-SCRAPER-EXPORTS/instance_1",
                str(export["csv_downloads_path"]),
            )
            with patch.object(db, "SQLITE_DB_PATH", database_path):
                self.assertEqual(
                    db.fetch_collection_run_by_id(
                        first,
                        backend="sqlite",
                    )["status"],
                    "stopped_by_user",
                )

    def test_zero_rows_is_empty_export_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "hotel_price_collector.sqlite"
            run_id = create_running_run(database_path, 0)
            save_schedule(
                "near_30_days",
                default_schedule("near_30_days"),
                data_dir=root,
            )
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "update collection_runs set status = 'completed_all_dates' "
                    "where id = ?",
                    (run_id,),
                )
            with patch.object(db, "SQLITE_DB_PATH", database_path):
                export = automatic_export_run_csv(
                    run_id,
                    database_path=database_path,
                    instance_id="near_30_days",
                    copy_to_downloads=False,
                )
            self.assertEqual(export["csv_export_status"], "empty_export")
            self.assertIsNone(export["csv_file_path"])
            status = json.loads(
                (
                    root / "status" / "current_job_status.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(status["local_export_status"], "empty_export")


class DriveAndWorkspaceContractTests(unittest.TestCase):
    def test_exact_drive_folder_mapping(self):
        self.assertEqual(
            {
                key: value.drive_folder_id
                for key, value in SCHEDULED_INSTANCES.items()
            },
            {
                "near_30_days": "18kkxujFodzEfoOmhfpBuZI8xDzaoUk8b",
                "medium_31_120_days": "1ATZztmP0pS9c0oprzF3LzQKYcpsXtT7S",
                "long_121_365_days": "19TNxbw6-ymHmUE2dkrSLyvKwYtjP4H5c",
            },
        )

    def test_unconfigured_drive_preserves_pending_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "result.csv"
            csv_path.write_text("a\n1\n", encoding="utf-8")
            schedule = default_schedule("near_30_days")
            schedule["drive_upload_enabled"] = True
            schedule["upload_excel"] = False
            save_schedule("near_30_days", schedule, data_dir=root)
            run = {
                "id": 9,
                "status": "completed_all_dates",
                "completed_at": "2026-07-25T12:00:00+02:00",
                "source": "Demo",
                "city_or_region": "Orlando",
                "authoritative_row_count": 1,
                "csv_rows_exported": 1,
            }
            with patch(
                "services.drive_delivery._rclone_binary",
                return_value=None,
            ):
                state = upload_run_bundle(
                    "near_30_days",
                    run,
                    data_dir=root,
                    csv_path=csv_path,
                    upload_excel=False,
                )
            self.assertEqual(state["drive_upload_status"], "pending")
            self.assertTrue(csv_path.is_file())

    def test_workspace_lookup_exact_then_case_insensitive(self):
        listing = "\n".join(
            (
                "0  * DG: 1920x1080 VP: 0,0 WA: 0,0 1920x1040 Workspace 1",
                "1  - DG: 1920x1080 VP: 0,0 WA: 0,0 1920x1040 SCRAPER 1",
            )
        )
        self.assertEqual(
            resolve_workspace_index(listing, "SCRAPER 1"),
            (1, "SCRAPER 1"),
        )
        self.assertEqual(
            resolve_workspace_index(listing, "scraper 1"),
            (1, "SCRAPER 1"),
        )

    def test_unrelated_chrome_window_is_never_selected(self):
        listing = "\n".join(
            (
                "0x01 0 111 host google-chrome.Google-chrome Personal Chrome",
                "0x02 0 222 host "
                "ota-scraper-instance-1.ota-scraper-instance-1 Booking.com",
                "0x03 0 333 host chromium.Chromium Booking popup",
            )
        )
        self.assertEqual(
            select_owned_window_ids(
                listing,
                owned_pids={222, 333},
                window_class="ota-scraper-instance-1",
            ),
            ["0x02", "0x03"],
        )
        self.assertNotIn(
            "0x01",
            select_owned_window_ids(
                listing,
                owned_pids={222, 333},
                window_class="ota-scraper-instance-1",
            ),
        )

    def test_required_operational_fields_are_complete(self):
        expected = {
            "scheduler_enabled",
            "runs_per_day",
            "next_scheduled_run",
            "schedule_timezone",
            "schedule_start_date",
            "schedule_end_date",
            "stop_requested",
            "stop_requested_at",
            "stop_source",
            "local_export_status",
            "local_export_path",
            "local_export_rows",
            "local_export_bytes",
            "google_drive_upload_status",
            "google_drive_folder_id",
            "google_drive_remote_filename",
            "google_drive_remote_bytes",
            "google_drive_upload_error",
            "workspace_requested",
            "workspace_detected",
            "browser_window_id",
            "browser_window_workspace",
            "workspace_move_status",
        }
        self.assertEqual(set(REQUIRED_OPERATIONAL_FIELDS), expected)


if __name__ == "__main__":
    unittest.main()
