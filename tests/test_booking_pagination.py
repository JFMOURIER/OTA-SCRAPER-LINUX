from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlparse

from collectors.base import CollectorOptions
from collectors.booking_playwright import BookingPlaywrightCollector
from services.booking_pagination import (
    NORMAL_PAGE_SIZE,
    PaginationResourceSnapshot,
    PaginationResults,
    evaluate_pagination_resources,
    next_resume_offset,
    url_with_offset,
)
from services.job_runner import proven_completed_dates, update_checkpoint_date
from services.scraper_errors import PaginationUnsupportedError, ResourcePressureError


BASE_URL = (
    "https://www.booking.com/searchresults.html?ss=Orlando"
    "&checkin=2026-09-10&checkout=2026-09-11"
    "&group_adults=2&selected_currency=USD&order=price"
)
CHECKIN = date(2026, 9, 10)
CHECKOUT = date(2026, 9, 11)


def hotel(number: int, *, query: str = "") -> dict[str, object]:
    suffix = f"?{query}" if query else ""
    return {
        "hotel_name": f"Hotel {number}",
        "hotel_url": f"https://www.booking.com/hotel/us/hotel-{number}.html{suffix}",
        "collection_status": "success",
        "checkin_date": CHECKIN,
        "checkout_date": CHECKOUT,
        "source": "Booking.com",
        "city_or_region": "Orlando",
    }


class FakePage:
    def __init__(self, pages: dict[int, list[dict[str, object]]], events: list[tuple]) -> None:
        self.pages = pages
        self.events = events
        self.offset = 0
        self.url = BASE_URL

    def goto(self, url: str, **_kwargs) -> None:
        self.url = url
        self.offset = int(dict(parse_qsl(urlparse(url).query)).get("offset", 0))
        self.events.append(("goto", self.offset))

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


class HeadingNode:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self, **_kwargs) -> str:
        return self.text


class HeadingLocator:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts

    def count(self) -> int:
        return len(self.texts)

    def nth(self, index: int) -> HeadingNode:
        return HeadingNode(self.texts[index])


class HeadingPage:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts

    def locator(self, _selector: str) -> HeadingLocator:
        return HeadingLocator(self.texts)


class BookingPaginationTests(unittest.TestCase):
    def _run(
        self,
        pages: dict[int, list[dict[str, object]]],
        *,
        maximum: int = 500,
        options: CollectorOptions | None = None,
        partial_events: list[tuple] | None = None,
    ) -> tuple[list[dict], CollectorOptions, list[tuple]]:
        collector = BookingPlaywrightCollector()
        events: list[tuple] = []
        page = FakePage(pages, events)
        options = options or CollectorOptions(
            screenshots_enabled=False,
            disable_filters_during_complete_collection=True,
            resource_check_callback=lambda: ("ok", {"reason": "test"}),
        )
        if options.resource_check_callback is None:
            options.resource_check_callback = lambda: ("ok", {"reason": "test"})
        if partial_events is not None:
            options.partial_results_callback = (
                lambda _date, rows, metadata: partial_events.append(
                    ("partial", metadata["pagination_offset"], len(rows))
                )
            )

        def extract(_page, _selector, _count, *_args, ranking_offset=0, **_kwargs):
            events.append(("extract", page.offset, ranking_offset))
            return [dict(row) for row in page.pages.get(page.offset, [])]

        with (
            patch.object(
                collector,
                "_extract_booking_visible_result_count_details",
                return_value={"count": None, "text": None, "is_lower_bound": False},
            ),
            patch.object(
                collector,
                "_valid_card_count",
                side_effect=lambda _page, _selector: len(page.pages.get(page.offset, [])),
            ),
            patch.object(collector, "_bulk_extract_cards", side_effect=extract),
            patch.object(collector, "_wait_for_any_card_selector"),
            patch.object(collector, "_verify_search_results_or_raise"),
            patch.object(collector, "_ensure_currency"),
            patch.object(collector, "_bottom_visible_text", return_value=""),
        ):
            rows = collector._collect_offset_pages(
                page=page,
                selector="[data-testid='property-card']",
                base_url=BASE_URL,
                city_or_region="Orlando",
                checkin_date=CHECKIN,
                checkout_date=CHECKOUT,
                number_of_nights=1,
                adults=2,
                currency="USD",
                max_hotels=maximum,
                options=options,
                progress_callback=None,
                log_callback=None,
                screenshot_path=None,
            )
        return rows, options, events

    def test_offsets_progress_and_each_page_is_extracted_before_navigation(self) -> None:
        pages = {
            offset: [hotel(offset + index) for index in range(NORMAL_PAGE_SIZE)]
            for offset in (0, 25, 50, 75)
        }
        rows, options, events = self._run(pages, maximum=100)
        self.assertEqual(options.stats["pagination_offsets_visited"], [0, 25, 50, 75])
        self.assertEqual(len(rows), 100)
        for offset, next_offset in ((0, 25), (25, 50), (50, 75)):
            self.assertLess(
                events.index(("extract", offset, offset)),
                events.index(("goto", next_offset)),
            )

    def test_duplicates_across_pages_are_deduplicated_by_canonical_url(self) -> None:
        pages = {
            0: [hotel(index, query="aid=one") for index in range(25)],
            25: [hotel(index, query="aid=two") for index in range(20, 45)],
            50: [hotel(index) for index in range(45, 50)],
        }
        rows, options, _events = self._run(pages)
        self.assertEqual(len(rows), 50)
        self.assertEqual(options.stats["completion_status"], "completed_results_exhausted")
        self.assertTrue(all("?" not in str(row["hotel_url"]) for row in rows))

    def test_max_hotels_is_a_ceiling(self) -> None:
        pages = {0: [hotel(index) for index in range(25)]}
        rows, options, _events = self._run(pages, maximum=20)
        self.assertEqual(len(rows), 20)
        self.assertEqual(options.stats["completion_status"], "completed_target_reached")

    def test_short_final_page_stops_as_results_exhausted(self) -> None:
        pages = {
            0: [hotel(index) for index in range(25)],
            25: [hotel(25 + index) for index in range(7)],
        }
        rows, options, _events = self._run(pages)
        self.assertEqual(len(rows), 32)
        self.assertEqual(options.stats["pagination_offsets_visited"], [0, 25])
        self.assertEqual(options.stats["pagination_stop_reason"], "short_final_page")

    def test_page_with_no_new_unique_records_stops(self) -> None:
        pages = {
            0: [hotel(index) for index in range(25)],
            25: [
                hotel(index, query="duplicate=yes")
                for index in reversed(range(25))
            ],
        }
        rows, options, _events = self._run(pages)
        self.assertEqual(len(rows), 25)
        self.assertEqual(options.stats["pagination_stop_reason"], "no_new_unique_hotels")

    def test_identical_offset_page_is_not_falsely_completed(self) -> None:
        pages = {
            0: [hotel(index) for index in range(25)],
            25: [hotel(index, query="tracking=changed") for index in range(25)],
        }
        options = CollectorOptions(
            screenshots_enabled=False,
            disable_filters_during_complete_collection=True,
            resource_check_callback=lambda: ("ok", {"reason": "test"}),
        )
        with self.assertRaises(PaginationUnsupportedError):
            self._run(pages, options=options)
        self.assertEqual(
            options.stats["completion_status"],
            "incomplete_pagination_unsupported",
        )

    def test_first_page_executes_without_load_more_growth_timer(self) -> None:
        rows, options, events = self._run(
            {0: [hotel(index) for index in range(25)]},
            maximum=10,
        )
        self.assertEqual(len(rows), 10)
        self.assertIn(("extract", 0, 0), events)
        self.assertEqual(options.stats["completion_status"], "completed_target_reached")

    def test_soft_resource_stop_preserves_partial_and_is_not_complete(self) -> None:
        partial_events: list[tuple] = []
        options = CollectorOptions(
            screenshots_enabled=False,
            disable_filters_during_complete_collection=True,
            resource_check_callback=lambda: (
                "soft",
                {"reason": "low headroom", "available_ram_mb": 800},
            ),
        )
        with self.assertRaises(ResourcePressureError):
            self._run(
                {0: [hotel(index) for index in range(25)]},
                options=options,
                partial_events=partial_events,
            )
        self.assertEqual(partial_events, [("partial", 0, 25)])
        self.assertEqual(options.stats["completion_status"], "incomplete_resource_soft_limit")

    def test_resume_loads_partial_and_continues_at_next_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            partial_dir = Path(directory)
            initial = [hotel(index) for index in range(49)]
            (partial_dir / f"{CHECKIN.isoformat()}_partial_hotels.json").write_text(
                json.dumps(initial, default=str),
                encoding="utf-8",
            )
            options = CollectorOptions(
                screenshots_enabled=False,
                disable_filters_during_complete_collection=True,
                partial_dir=partial_dir,
                resume_partial_results=True,
                resource_check_callback=lambda: ("ok", {"reason": "test"}),
            )
            rows, options, events = self._run(
                {50: [hotel(49)]},
                options=options,
            )
        self.assertEqual(next_resume_offset(49), 50)
        self.assertIn(("goto", 50), events)
        self.assertEqual(len(rows), 50)
        self.assertEqual(options.stats["completion_status"], "completed_results_exhausted")

    def test_resume_does_not_complete_when_offset_returns_old_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            partial_dir = Path(directory)
            initial = [hotel(index) for index in range(49)]
            (partial_dir / f"{CHECKIN.isoformat()}_partial_hotels.json").write_text(
                json.dumps(initial, default=str),
                encoding="utf-8",
            )
            options = CollectorOptions(
                screenshots_enabled=False,
                disable_filters_during_complete_collection=True,
                partial_dir=partial_dir,
                resume_partial_results=True,
                resource_check_callback=lambda: ("ok", {"reason": "test"}),
            )
            with self.assertRaises(PaginationUnsupportedError):
                self._run(
                    {50: [hotel(index) for index in range(25)]},
                    options=options,
                )
        self.assertEqual(
            options.stats["completion_status"],
            "incomplete_pagination_unsupported",
        )

    def test_impossible_unverified_result_count_is_rejected(self) -> None:
        collector = BookingPlaywrightCollector()
        impossible = collector._extract_booking_visible_result_count_details(
            HeadingPage(["Orlando: 25,309 properties found"]),
            expected_city="Orlando",
        )
        valid = collector._extract_booking_visible_result_count_details(
            HeadingPage(["Orlando: 313 properties found"]),
            expected_city="Orlando",
        )
        self.assertIsNone(impossible["count"])
        self.assertEqual(valid["count"], 313)

    def test_resource_limited_date_remains_incomplete_and_resumable(self) -> None:
        checkpoint = {
            "completed_dates": [CHECKIN.isoformat()],
            "date_statuses": {},
            "dates": {},
        }
        update_checkpoint_date(
            checkpoint,
            stay_date=CHECKIN,
            checkout_date=CHECKOUT,
            status="stopped_resource_limit",
            records_collected=0,
        )
        self.assertNotIn(CHECKIN.isoformat(), checkpoint["completed_dates"])
        self.assertEqual(proven_completed_dates(checkpoint, {}), set())

    def test_only_proven_completed_dates_are_skipped(self) -> None:
        checkpoint = {
            "date_statuses": {
                "2026-09-10": {"status": "completed_target_reached"},
                "2026-09-11": {"status": "stopped_resource_limit"},
                "2026-09-12": {"status": "completed_results_exhausted"},
            }
        }
        completed = proven_completed_dates(
            checkpoint,
            {"2026-09-10": 150, "2026-09-11": 75},
        )
        self.assertEqual(completed, {"2026-09-10", "2026-09-12"})

    def test_high_swap_alone_is_warning_not_resource_stop(self) -> None:
        snapshot = PaginationResourceSnapshot(
            available_ram_mb=2386.9,
            swap_free_mb=219.6,
            swap_percent=94.6,
            browser_rss_mb=1885.5,
            browser_pss_mb=1600.0,
            browser_process_count=10,
        )
        level, _reason = evaluate_pagination_resources(snapshot)
        self.assertEqual(level, "warning")

    def test_sqlite_insert_is_idempotent_for_canonical_pagination_rows(self) -> None:
        import database.db as database

        with tempfile.TemporaryDirectory() as directory:
            test_db = Path(directory) / "canary.sqlite"
            original_path = database.SQLITE_DB_PATH
            try:
                database.SQLITE_DB_PATH = test_db
                database.init_db("sqlite")
                row = PaginationResults([hotel(1, query="aid=first")]).values()[0]
                row["collection_run_id"] = 9001
                database.insert_hotel_results([row], backend="sqlite")
                updated = PaginationResults([hotel(1, query="aid=second")]).values()[0]
                updated["collection_run_id"] = 9001
                database.insert_hotel_results([updated], backend="sqlite")
                with sqlite3.connect(test_db) as connection:
                    count = connection.execute(
                        "select count(*) from hotel_price_results where collection_run_id = 9001"
                    ).fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                database.SQLITE_DB_PATH = original_path

    def test_url_offset_preserves_search_configuration(self) -> None:
        paged = url_with_offset(BASE_URL, 75)
        query = dict(parse_qsl(urlparse(paged).query))
        self.assertEqual(query["offset"], "75")
        self.assertEqual(query["ss"], "Orlando")
        self.assertEqual(query["checkin"], "2026-09-10")
        self.assertEqual(query["checkout"], "2026-09-11")
        self.assertEqual(query["group_adults"], "2")
        self.assertEqual(query["selected_currency"], "USD")
        self.assertEqual(query["order"], "price")


if __name__ == "__main__":
    unittest.main()
