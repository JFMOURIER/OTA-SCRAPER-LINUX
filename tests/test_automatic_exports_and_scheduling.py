from __future__ import annotations

import csv
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import openpyxl

import database.db as db
from services.consolidation import consolidate_latest_full_year
from services.exporter import (
    CSV_EXPORT_COLUMNS,
    build_run_excel_filename,
    export_sqlite_run_to_excel,
    next_available_run_excel_filename,
)
from services.resource_guard import (
    ConcurrencyUpgradeNotReady,
    HostConcurrencyLimitReached,
    HostWorkerSemaphore,
    ScraperAlreadyRunning,
    SingleScraperLock,
)
from services.run_exports import automatic_export_run_csv
from services.scheduled_instances import (
    SCHEDULED_INSTANCES,
    ScheduledInstanceDefinition,
    resolved_windows,
    validate_contiguous_non_overlapping_windows,
)


def hotel_rows(run_id: int, count: int) -> list[dict]:
    return [
        {
            "collection_run_id": run_id,
            "source": "Demo",
            "city_or_region": "Orlando",
            "hotel_name": f"Hotel {index}",
            "ota_hotel_id": str(index),
            "hotel_url": f"https://example.test/hotel/{index}",
            "raw_price_text": f"US$ {100 + index}",
            "parsed_price": 100 + index,
            "cheapest_price_total": 100 + index,
            "currency": "USD",
            "checkin_date": date(2026, 8, 1),
            "checkout_date": date(2026, 8, 2),
            "number_of_nights": 1,
            "adults": 2,
            "collection_status": "success",
            "collected_at": datetime(2026, 7, 24, 12, 0),
        }
        for index in range(count)
    ]


class AutomaticExportTests(unittest.TestCase):
    def _create_run(self, database_path: Path, rows: int) -> int:
        with patch.object(db, "SQLITE_DB_PATH", database_path):
            db.init_db("sqlite")
            run_id = db.create_collection_run(
                "Demo",
                "Orlando",
                date(2026, 8, 1),
                date(2026, 8, 2),
                1,
                2,
                "USD",
                rows,
                backend="sqlite",
            )
            for offset in range(0, rows, 200):
                db.insert_hotel_results(
                    hotel_rows(run_id, rows)[offset : offset + 200],
                    backend="sqlite",
                )
            return run_id

    def test_automatic_csv_exceeds_preview_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "hotel_price_collector.sqlite"
            run_id = self._create_run(database_path, 620)
            with patch.object(
                db,
                "SQLITE_DB_PATH",
                database_path,
            ), patch.dict(
                "os.environ",
                {
                    "OTA_AUTO_EXPORT_ENABLED": "1",
                    "OTA_AUTO_EXPORT_DOWNLOADS": "0",
                },
            ):
                first = db.update_collection_run_status(
                    run_id,
                    "completed_all_dates",
                    backend="sqlite",
                )
                second = db.update_collection_run_status(
                    run_id,
                    "completed_all_dates",
                    backend="sqlite",
                )
                metadata = db.fetch_collection_run_by_id(
                    run_id,
                    backend="sqlite",
                )
            self.assertIsNotNone(metadata)
            self.assertEqual(first["csv_export_status"], "succeeded")
            self.assertEqual(second["csv_file_path"], first["csv_file_path"])
            self.assertEqual(metadata["csv_rows_exported"], 620)
            csv_path = Path(metadata["csv_file_path"])
            self.assertIn(f"_run_{run_id}_", csv_path.name)
            self.assertIn("_final_", csv_path.name)
            self.assertEqual(len(list(csv_path.parent.glob("*.csv"))), 1)
            with csv_path.open("rb") as handle:
                self.assertEqual(handle.read(3), b"\xef\xbb\xbf")
            with csv_path.open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as handle:
                reader = csv.DictReader(handle)
                self.assertTrue(set(db.RESULT_FIELDS).issubset(reader.fieldnames))
                self.assertEqual(len(list(reader)), 620)

    def test_partial_csv_and_atomic_download_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "instance" / "hotel_price_collector.sqlite"
            database_path.parent.mkdir()
            downloads = root / "Downloads"
            run_id = self._create_run(database_path, 3)
            with patch.object(
                db,
                "SQLITE_DB_PATH",
                database_path,
            ), patch.dict(
                "os.environ",
                {
                    "OTA_AUTO_EXPORT_ENABLED": "0",
                    "OTA_AUTO_EXPORT_DOWNLOADS": "0",
                },
            ):
                db.update_collection_run_status(
                    run_id,
                    "stopped_by_user_with_partial_results",
                    backend="sqlite",
                )
                payload = automatic_export_run_csv(
                    run_id,
                    database_path=database_path,
                    instance_id="near_30_days",
                    downloads_dir=downloads,
                    copy_to_downloads=True,
                )
            self.assertEqual(payload["csv_export_status"], "succeeded")
            self.assertIn("_partial_", Path(payload["csv_file_path"]).name)
            self.assertTrue(Path(payload["csv_downloads_path"]).is_file())
            self.assertEqual(
                Path(payload["csv_file_path"]).read_bytes(),
                Path(payload["csv_downloads_path"]).read_bytes(),
            )
            self.assertEqual(list(downloads.glob("*.tmp*")), [])
            Path(payload["csv_downloads_path"]).unlink()
            with patch.object(db, "SQLITE_DB_PATH", database_path):
                repaired = automatic_export_run_csv(
                    run_id,
                    database_path=database_path,
                    instance_id="near_30_days",
                    downloads_dir=downloads,
                    copy_to_downloads=True,
                )
            self.assertEqual(
                repaired["csv_file_path"],
                payload["csv_file_path"],
            )
            self.assertEqual(
                len(list(Path(payload["csv_file_path"]).parent.glob("*.csv"))),
                1,
            )
            self.assertTrue(Path(repaired["csv_downloads_path"]).is_file())

    def test_explicit_run_excel_uses_all_sqlite_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "hotel_price_collector.sqlite"
            run_id = self._create_run(database_path, 620)
            filename = build_run_excel_filename(
                "Demo",
                "Orlando",
                "near_30_days",
                run_id,
                date(2026, 8, 1),
                date(2026, 8, 1),
                datetime(2026, 7, 24, 12, 0),
            )
            with patch.object(db, "SQLITE_DB_PATH", database_path):
                excel_path = export_sqlite_run_to_excel(
                    run_id,
                    {
                        "source": "Demo",
                        "city_or_region": "Orlando",
                        "checkin_date": "2026-08-01",
                        "number_of_nights": 1,
                    },
                    root / "exports",
                    filename=filename,
                )
            workbook = openpyxl.load_workbook(excel_path, read_only=True)
            worksheet = workbook["All Hotel Results"]
            exported_rows = sum(
                1 for _ in worksheet.iter_rows(values_only=True)
            ) - 1
            workbook.close()
            self.assertEqual(exported_rows, 620)

    def test_manual_excel_filename_advances_instead_of_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build_run_excel_filename(
                "Demo",
                "Orlando",
                "near_30_days",
                7,
                date(2026, 8, 1),
                date(2026, 8, 1),
                datetime(2026, 7, 24, 12, 0, 0),
            )
            (root / first).touch()
            second = next_available_run_excel_filename(
                root,
                "Demo",
                "Orlando",
                "near_30_days",
                7,
                date(2026, 8, 1),
                date(2026, 8, 1),
                now=datetime(2026, 7, 24, 12, 0, 0),
            )
            self.assertNotEqual(first, second)
            self.assertIn("20260724_120001", second)


class IsolationAndSchedulingTests(unittest.TestCase):
    def test_instance_databases_profiles_locks_and_ports_are_independent(self):
        definitions = list(SCHEDULED_INSTANCES.values())
        self.assertEqual(
            [definition.port for definition in definitions],
            [8501, 8502, 8503],
        )
        self.assertEqual(
            len({definition.data_dir for definition in definitions}),
            3,
        )
        self.assertEqual(
            len(
                {
                    definition.data_dir / "hotel_price_collector.sqlite"
                    for definition in definitions
                }
            ),
            3,
        )
        self.assertEqual(
            len(
                {
                    definition.data_dir / "browser_profile"
                    for definition in definitions
                }
            ),
            3,
        )
        self.assertEqual(
            len(
                {
                    definition.data_dir / "status" / "active_scraper.lock"
                    for definition in definitions
                }
            ),
            3,
        )
        self.assertEqual(
            len(
                {
                    definition.data_dir / "partial"
                    for definition in definitions
                }
            ),
            3,
        )

    def test_dynamic_windows_are_contiguous_without_overlap(self):
        anchor = date(2026, 7, 24)
        windows = resolved_windows(anchor)
        self.assertEqual(
            windows["near_30_days"],
            (date(2026, 7, 24), date(2026, 8, 23)),
        )
        self.assertEqual(
            windows["medium_31_120_days"],
            (date(2026, 8, 24), date(2026, 11, 23)),
        )
        self.assertEqual(
            windows["long_121_365_days"],
            (date(2026, 11, 24), date(2027, 7, 23)),
        )
        self.assertTrue(validate_contiguous_non_overlapping_windows(anchor))

    def test_three_instance_locks_do_not_conflict_but_same_lock_skips(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locks = [
                SingleScraperLock(root / name / "active.lock")
                for name in SCHEDULED_INSTANCES
            ]
            for index, lock in enumerate(locks):
                lock.acquire(f"job-{index}")
            try:
                duplicate = SingleScraperLock(
                    root / "near_30_days" / "active.lock"
                )
                with self.assertRaises(ScraperAlreadyRunning):
                    duplicate.acquire("scheduled-overlap")
            finally:
                for lock in locks:
                    lock.release()

    def test_host_semaphore_defaults_to_one_and_can_be_three_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = HostWorkerSemaphore(root, maximum_workers=1)
            second = HostWorkerSemaphore(root, maximum_workers=1)
            first.acquire(instance_id="near", job_id="1")
            try:
                with self.assertRaises(HostConcurrencyLimitReached):
                    second.acquire(instance_id="medium", job_id="2")
            finally:
                first.release()
            semaphores = [
                HostWorkerSemaphore(root, maximum_workers=3)
                for _ in range(3)
            ]
            try:
                for index, semaphore in enumerate(semaphores):
                    semaphore.acquire(
                        instance_id=f"instance-{index}",
                        job_id=str(index),
                    )
                self.assertEqual(
                    {semaphore.slot for semaphore in semaphores},
                    {1, 2, 3},
                )
            finally:
                for semaphore in semaphores:
                    semaphore.release()

    def test_three_worker_environment_is_gated_by_hardware_marker(self):
        with patch.dict(
            "os.environ",
            {"OTA_MAX_CONCURRENT_WORKERS": "3"},
        ), patch(
            "services.hardware_preflight.three_worker_concurrency_enabled",
            return_value=False,
        ):
            with self.assertRaises(ConcurrencyUpgradeNotReady):
                HostWorkerSemaphore(Path("/tmp/unused-host-semaphore-test"))

    def test_systemd_timers_and_scheduled_skip_contract(self):
        root = Path(__file__).resolve().parents[1]
        expected = {
            "ota-scraper-near.timer": "OnCalendar=*-*-* *:05:00",
            "ota-scraper-medium.timer": "OnCalendar=*-*-* 00,12:20:00",
            "ota-scraper-long.timer": "OnCalendar=*-*-* 01:35:00",
        }
        for filename, schedule in expected.items():
            text = (root / "systemd" / filename).read_text(
                encoding="utf-8"
            )
            self.assertIn(schedule, text)
            self.assertIn("Persistent=false", text)
        scheduled_runner = (
            root / "tools" / "ota_scheduled_run.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "scheduled_run_skipped_previous_run_active",
            scheduled_runner,
        )
        for name in ("near", "medium", "long"):
            service = (
                root / "systemd" / f"ota-scraper-{name}.service"
            ).read_text(encoding="utf-8")
            self.assertIn("Environment=OTA_MAX_CONCURRENT_WORKERS=1", service)
            self.assertIn("Restart=on-failure", service)


class ConsolidationTests(unittest.TestCase):
    def _instance(
        self,
        root: Path,
        instance_id: str,
        port: int,
        row: dict[str, str],
        run_id: int,
    ) -> ScheduledInstanceDefinition:
        data_dir = root / instance_id
        exports = data_dir / "exports"
        exports.mkdir(parents=True)
        csv_path = exports / f"run_{run_id}.csv"
        with csv_path.open(
            "w",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_EXPORT_COLUMNS)
            writer.writeheader()
            writer.writerow(row)
        database_path = data_dir / "hotel_price_collector.sqlite"
        connection = sqlite3.connect(database_path)
        connection.execute(
            """
            CREATE TABLE collection_runs (
                id INTEGER PRIMARY KEY,
                status TEXT,
                completed_at TEXT,
                csv_export_status TEXT,
                csv_file_path TEXT,
                csv_exported_at TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO collection_runs VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                "completed_all_dates",
                row["timestamp"],
                "succeeded",
                str(csv_path.resolve()),
                row["timestamp"],
            ),
        )
        connection.commit()
        connection.close()
        return ScheduledInstanceDefinition(
            instance_id=instance_id,
            port=port,
            display=f":{port}",
            drive_folder_id=f"folder-{instance_id}",
            drive_folder_url=f"https://example.test/{instance_id}",
            default_frequency_mode="daily",
            default_interval_minutes=None,
            default_runs_per_day=1,
            default_daily_run_times=("00:00",),
            data_dir_override=data_dir,
        )

    def test_consolidation_keeps_most_recent_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = {
                column: ""
                for column in CSV_EXPORT_COLUMNS
            }
            common.update(
                {
                    "source_website": "Booking.com",
                    "hotel_name": "Same Hotel",
                    "ota_hotel_id": "42",
                    "currency": "USD",
                    "adults": "2",
                    "checkin_date": "2026-08-01",
                    "checkout_date": "2026-08-02",
                    "parsed_price": "100",
                }
            )
            near_row = {
                **common,
                "timestamp": "2026-07-24 08:00:00",
                "collection_run_id": "1",
                "scrape_session_id": "1",
                "instance_id": "near_30_days",
            }
            medium_row = {
                **common,
                "parsed_price": "90",
                "timestamp": "2026-07-24 09:00:00",
                "collection_run_id": "2",
                "scrape_session_id": "2",
                "instance_id": "medium_31_120_days",
            }
            long_row = {
                **common,
                "hotel_name": "Other Hotel",
                "ota_hotel_id": "99",
                "checkin_date": "2027-01-01",
                "checkout_date": "2027-01-02",
                "timestamp": "2026-07-24 07:00:00",
                "collection_run_id": "3",
                "scrape_session_id": "3",
                "instance_id": "long_121_365_days",
            }
            definitions = [
                self._instance(root, "near_30_days", 8501, near_row, 1),
                self._instance(
                    root,
                    "medium_31_120_days",
                    8502,
                    medium_row,
                    2,
                ),
                self._instance(
                    root,
                    "long_121_365_days",
                    8503,
                    long_row,
                    3,
                ),
            ]
            output = consolidate_latest_full_year(
                definitions=definitions,
                output_dir=root / "Downloads",
                now=datetime(2026, 7, 24, 10, 0),
            )
            with output.open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            same_hotel = next(
                row for row in rows if row["ota_hotel_id"] == "42"
            )
            self.assertEqual(same_hotel["parsed_price"], "90")
            self.assertEqual(
                same_hotel["collection_instance"],
                "medium_31_120_days",
            )
            self.assertEqual(same_hotel["source_run_id"], "2")
            self.assertEqual(
                set(
                    [
                        "collection_instance",
                        "source_run_id",
                        "collection_timestamp",
                        "date_bucket",
                    ]
                ).issubset(rows[0]),
                True,
            )


if __name__ == "__main__":
    unittest.main()
