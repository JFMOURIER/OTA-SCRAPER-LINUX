from __future__ import annotations

import ast
import hashlib
import tempfile
import unittest
from datetime import date
from pathlib import Path

import database.db as database
from database.db import result_aggregates_by_run_id


ROOT = Path(__file__).resolve().parents[1]
CHECKIN = date(2026, 8, 1)
CHECKOUT = date(2026, 8, 2)


class AppDatabaseImportContractTests(unittest.TestCase):
    def test_every_database_symbol_imported_by_app_exists(self) -> None:
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "database.db"
            for alias in node.names
        }
        self.assertIn("result_aggregates_by_run_id", imported_names)
        self.assertEqual(
            [name for name in sorted(imported_names) if not hasattr(database, name)],
            [],
        )
        self.assertTrue(callable(result_aggregates_by_run_id))


class ResultAggregateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database_path = database.SQLITE_DB_PATH
        database.SQLITE_DB_PATH = (
            Path(self.temporary_directory.name) / "aggregate.sqlite"
        )
        database.init_db("sqlite")

    def tearDown(self) -> None:
        database.SQLITE_DB_PATH = self.original_database_path
        self.temporary_directory.cleanup()

    def create_run(self) -> int:
        return database.create_collection_run(
            "Booking.com",
            "Orlando",
            CHECKIN,
            CHECKOUT,
            1,
            2,
            "USD",
            600,
            backend="sqlite",
        )

    @staticmethod
    def hotel(
        run_id: int,
        index: int,
        *,
        checkin: date = CHECKIN,
        raw_price: str | None = None,
        parsed_price: float | None = None,
        cheapest_price: float | None = None,
    ) -> dict:
        return {
            "collection_run_id": run_id,
            "source": "Booking.com",
            "city_or_region": "Orlando",
            "hotel_name": f"Hotel {index}",
            "hotel_url": f"https://www.booking.com/hotel/us/hotel-{index}.html",
            "checkin_date": checkin,
            "checkout_date": date.fromordinal(checkin.toordinal() + 1),
            "number_of_nights": 1,
            "adults": 2,
            "currency": "USD",
            "raw_price_text": raw_price,
            "parsed_price": parsed_price,
            "cheapest_price_total": cheapest_price,
            "collection_status": "success",
        }

    def test_populated_run_returns_price_quality_and_date_totals(self) -> None:
        run_id = self.create_run()
        database.insert_hotel_results(
            [
                self.hotel(
                    run_id,
                    1,
                    raw_price="US$100",
                    parsed_price=100.0,
                    cheapest_price=100.0,
                ),
                self.hotel(
                    run_id,
                    2,
                    checkin=date(2026, 8, 2),
                    raw_price="US$200",
                    cheapest_price=200.0,
                ),
                self.hotel(run_id, 3),
            ],
            backend="sqlite",
        )

        aggregate = result_aggregates_by_run_id(run_id, backend="sqlite")

        self.assertEqual(aggregate["total_observations"], 3)
        self.assertEqual(aggregate["unique_hotels"], 3)
        self.assertEqual(aggregate["completed_date_count"], 2)
        self.assertEqual(aggregate["rows_with_raw_price"], 2)
        self.assertEqual(aggregate["rows_with_parsed_price"], 2)
        self.assertEqual(aggregate["rows_missing_price"], 1)
        self.assertEqual(aggregate["minimum_price"], 100.0)
        self.assertEqual(aggregate["maximum_price"], 200.0)
        self.assertEqual(aggregate["average_price"], 150.0)

    def test_empty_run_returns_stable_zero_values(self) -> None:
        aggregate = result_aggregates_by_run_id(
            self.create_run(), backend="sqlite"
        )
        self.assertEqual(aggregate["total_observations"], 0)
        self.assertEqual(aggregate["unique_hotels"], 0)
        self.assertEqual(aggregate["rows_with_raw_price"], 0)
        self.assertEqual(aggregate["rows_with_parsed_price"], 0)
        self.assertEqual(aggregate["rows_missing_price"], 0)
        self.assertIsNone(aggregate["average_price"])

    def test_absent_and_unknown_run_ids_do_not_crash(self) -> None:
        absent = result_aggregates_by_run_id(None, backend="sqlite")
        unknown = result_aggregates_by_run_id(999999, backend="sqlite")
        self.assertEqual(absent, unknown)
        self.assertEqual(absent["total_observations"], 0)

    def test_more_than_500_rows_are_not_capped_by_preview_limit(self) -> None:
        run_id = self.create_run()
        database.insert_hotel_results(
            [
                self.hotel(
                    run_id,
                    index,
                    raw_price=f"US${100 + index}",
                    parsed_price=float(100 + index),
                    cheapest_price=float(100 + index),
                )
                for index in range(550)
            ],
            backend="sqlite",
        )
        preview = database.fetch_results_by_run_id_limited(
            run_id, 500, backend="sqlite"
        )
        aggregate = result_aggregates_by_run_id(run_id, backend="sqlite")
        self.assertEqual(len(preview), 500)
        self.assertEqual(aggregate["total_observations"], 550)
        self.assertEqual(aggregate["rows_with_parsed_price"], 550)

    def test_aggregate_read_does_not_mutate_sqlite_database(self) -> None:
        run_id = self.create_run()
        database.insert_hotel_results(
            [
                self.hotel(
                    run_id,
                    1,
                    raw_price="US$100",
                    parsed_price=100.0,
                    cheapest_price=100.0,
                )
            ],
            backend="sqlite",
        )
        database.sqlite_wal_checkpoint("TRUNCATE")
        before = hashlib.sha256(
            database.SQLITE_DB_PATH.read_bytes()
        ).hexdigest()

        result_aggregates_by_run_id(run_id, backend="sqlite")

        after = hashlib.sha256(
            database.SQLITE_DB_PATH.read_bytes()
        ).hexdigest()
        self.assertEqual(after, before)
