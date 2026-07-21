from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import openpyxl

import database.db as db
from services.exporter import export_sqlite_run_to_csv, export_sqlite_run_to_excel


class PersistenceExportTests(unittest.TestCase):
    def test_resume_dedupe_and_atomic_streamed_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "test.sqlite"
            with patch.object(db, "SQLITE_DB_PATH", database_path):
                db.init_db("sqlite")
                run_id = db.create_collection_run(
                    "Demo", "Test City", date(2026, 8, 1), date(2026, 8, 2), 1, 2, "USD", 2, backend="sqlite"
                )
                rows = [
                    {
                        "collection_run_id": run_id,
                        "source": "Demo",
                        "city_or_region": "Test City",
                        "hotel_name": f"Hotel {index}",
                        "hotel_url": f"demo://hotel-{index}",
                        "checkin_date": date(2026, 8, 1),
                        "checkout_date": date(2026, 8, 2),
                        "number_of_nights": 1,
                        "adults": 2,
                        "currency": "USD",
                        "cheapest_price_total": 100 + index,
                        "collection_status": "success",
                        "collected_at": datetime(2026, 7, 21, 12, 0),
                    }
                    for index in (1, 2)
                ]
                self.assertEqual(db.insert_hotel_results(rows, backend="sqlite"), 2)
                self.assertEqual(db.insert_hotel_results(rows, backend="sqlite"), 2)
                self.assertEqual(db.count_results_by_run_id(run_id, backend="sqlite"), 2)
                self.assertEqual(db.sqlite_integrity_check(), "ok")

                summary = {
                    "source": "Demo",
                    "city_or_region": "Test City",
                    "checkin_date": date(2026, 8, 1),
                    "number_of_nights": 1,
                    "__date_status_rows": [{"checkin_date": "2026-08-01", "status": "completed"}],
                }
                excel_path = export_sqlite_run_to_excel(run_id, summary, root, "final.xlsx", batch_size=1)
                csv_path = export_sqlite_run_to_csv(run_id, summary, root, "final.csv", instance_id="test", batch_size=1)
                workbook = openpyxl.load_workbook(excel_path, read_only=True)
                self.assertEqual(
                    set(workbook.sheetnames),
                    {"All Results", "Errors", "Raw API Debug", "By Date Summary", "By Source Summary", "Date Status", "Run Summary"},
                )
                workbook.close()
                with csv_path.open(encoding="utf-8", newline="") as handle:
                    self.assertEqual(len(list(csv.DictReader(handle))), 2)
                self.assertEqual(list(root.glob("*.tmp*")), [])


if __name__ == "__main__":
    unittest.main()
