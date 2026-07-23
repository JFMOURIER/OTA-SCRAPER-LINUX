from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from unittest.mock import patch

from collectors.base import CollectorOptions
from collectors.booking_playwright import BookingPlaywrightCollector
from services.booking_network_batches import (
    batch_is_repeated,
    booking_batch_fingerprint,
    booking_response_fingerprint,
    is_booking_full_search_request,
    load_continuation,
    parse_booking_network_batch,
    request_payload_for_offset,
    response_has_access_restriction,
    save_continuation,
)
from services.booking_pagination import PaginationResults
from services.scraper_errors import AccessRestrictionError, PaginationUnsupportedError
from services.resource_guard import ResourceLimitExceeded
from services.job_runner import save_partial_records


CHECKIN = date(2026, 9, 10)
CHECKOUT = date(2026, 9, 11)
BASE_URL = (
    "https://www.booking.com/searchresults.html?ss=Orlando"
    "&checkin=2026-09-10&checkout=2026-09-11"
    "&group_adults=2&selected_currency=USD&order=price&nflt=ht_id%3D204"
)
REQUEST_URL = "https://www.booking.com/dml/graphql?lang=en-us"


def request_payload(offset: int = 75, rows_per_page: int = 25) -> dict:
    return {
        "operationName": "FullSearch",
        "variables": {
            "input": {
                "pagination": {
                    "offset": offset,
                    "rowsPerPage": rows_per_page,
                },
                "dates": {
                    "checkin": CHECKIN.isoformat(),
                    "checkout": CHECKOUT.isoformat(),
                },
            }
        },
    }


def response_payload(
    offset: int,
    *,
    count: int = 25,
    total: int = 300,
) -> dict:
    return {
        "data": {
            "searchQueries": {
                "search": {
                    "pagination": {
                        "nbResultsPerPage": 25,
                        "nbResultsTotal": total,
                    },
                    "results": [
                        {
                            "basicPropertyData": {
                                "id": offset + index,
                                "pageName": f"hotel-{offset + index}",
                                "location": {"countryCode": "us"},
                                "starRating": {"value": 3},
                                "reviewScore": {
                                    "score": "8.4",
                                    "reviewCount": str(100 + index),
                                },
                            },
                            "displayName": {"text": f"Hotel {offset + index}"},
                            "priceDisplayInfoIrene": {
                                "displayPrice": {
                                    "amountPerStay": {
                                        "amount": f"US${80 + offset + index}",
                                        "currency": "USD",
                                    }
                                }
                            },
                        }
                        for index in range(count)
                    ],
                }
            }
        }
    }


def parsed_batch(offset: int, *, count: int = 25, total: int = 300):
    return parse_booking_network_batch(
        response_payload(offset, count=count, total=total),
        offset=offset,
        rows_per_page=25,
    )


def hotel(number: int) -> dict:
    return {
        "hotel_name": f"Hotel {number}",
        "hotel_url": f"https://www.booking.com/hotel/us/hotel-{number}.html",
        "collection_status": "success",
        "checkin_date": CHECKIN,
        "checkout_date": CHECKOUT,
        "source": "Booking.com",
        "city_or_region": "Orlando",
    }


class FakeRequest:
    method = "POST"
    resource_type = "fetch"

    def __init__(self, payload: dict | None = None, url: str = REQUEST_URL) -> None:
        self.url = url
        self.post_data_json = payload


class FakePage:
    def __init__(self) -> None:
        self.url = BASE_URL
        self.goto_calls: list[str] = []

    def goto(self, url: str, **_kwargs) -> None:
        self.url = url
        self.goto_calls.append(url)


class FakeApiResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self.payload = payload
        self.disposed = False

    def json(self):
        return self.payload

    def body(self) -> bytes:
        return json.dumps(self.payload).encode()

    def dispose(self) -> None:
        self.disposed = True


class FakeRequestContext:
    def __init__(self, responses: list[FakeApiResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, _url: str, *, data: dict, **_kwargs):
        self.calls.append(data)
        return self.responses.pop(0)


class ReplayPage(FakePage):
    def __init__(self, request_context: FakeRequestContext) -> None:
        super().__init__()
        self.context = type("Context", (), {"request": request_context})()
        self.waits: list[int] = []

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


class BookingNetworkBatchTests(unittest.TestCase):
    def test_identifies_only_first_party_full_search_post(self) -> None:
        self.assertTrue(
            is_booking_full_search_request(FakeRequest(request_payload()))
        )
        self.assertFalse(
            is_booking_full_search_request(
                FakeRequest(request_payload(), "https://tracker.example/dml/graphql")
            )
        )
        wrong = request_payload()
        wrong["operationName"] = "OtherOperation"
        self.assertFalse(is_booking_full_search_request(FakeRequest(wrong)))

    def test_parses_json_batch_and_real_pagination_state(self) -> None:
        batch = parsed_batch(100)
        self.assertEqual(batch.offset, 100)
        self.assertEqual(batch.result_count, 25)
        self.assertEqual(batch.next_offset, 125)
        self.assertEqual(batch.total_results, 300)
        self.assertEqual(batch.properties[0]["hotel_name"], "Hotel 100")
        self.assertEqual(
            batch.properties[0]["hotel_url"],
            "https://www.booking.com/hotel/us/hotel-100.html",
        )
        self.assertEqual(batch.properties[0]["price_text"], "US$180")

    def test_request_replay_changes_only_continuation_offset(self) -> None:
        original = request_payload(75)
        replay = request_payload_for_offset(original, 125)
        self.assertEqual(
            replay["variables"]["input"]["pagination"]["offset"],
            125,
        )
        self.assertEqual(
            replay["variables"]["input"]["dates"],
            original["variables"]["input"]["dates"],
        )
        self.assertEqual(
            original["variables"]["input"]["pagination"]["offset"],
            75,
        )

    def test_repeated_batch_is_detected_by_hash_or_existing_keys(self) -> None:
        batch = parsed_batch(75)
        self.assertTrue(
            batch_is_repeated(
                batch,
                previous_hashes={batch.response_hash},
                existing_rows=[],
            )
        )
        rows = [
            {
                "hotel_name": item["hotel_name"],
                "hotel_url": item["hotel_url"],
            }
            for item in batch.properties
        ]
        self.assertTrue(
            batch_is_repeated(
                batch,
                previous_hashes=set(),
                existing_rows=rows,
            )
        )

    def test_response_fingerprint_ignores_duplicate_delivery_request(self) -> None:
        batch = parsed_batch(75)
        first_request = booking_batch_fingerprint(
            batch,
            request_url=REQUEST_URL,
            request_payload=request_payload(75),
        )
        stale_request = booking_batch_fingerprint(
            batch,
            request_url=REQUEST_URL,
            request_payload=request_payload(100),
        )
        self.assertNotEqual(first_request, stale_request)
        self.assertEqual(
            booking_response_fingerprint(batch),
            booking_response_fingerprint(parsed_batch(75)),
        )

    def test_same_offset_with_new_hotel_identities_is_not_repeated(self) -> None:
        original = parsed_batch(100)
        new_identities_same_offset = parse_booking_network_batch(
            response_payload(125),
            offset=100,
            rows_per_page=25,
        )
        existing_rows = [
            {
                "hotel_name": item["hotel_name"],
                "hotel_url": item["hotel_url"],
            }
            for item in original.properties
        ]
        self.assertFalse(
            batch_is_repeated(
                new_identities_same_offset,
                previous_hashes={original.response_hash},
                existing_rows=existing_rows,
            )
        )
        self.assertNotEqual(
            booking_response_fingerprint(original),
            booking_response_fingerprint(new_identities_same_offset),
        )

    def test_continuation_state_is_atomic_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = save_continuation(
                Path(directory),
                CHECKIN,
                next_offset=125,
                rows_per_page=25,
                response_hashes=("one", "two"),
                unique_records=125,
                completion_status="incomplete_network_batches",
            )
            loaded = load_continuation(Path(directory), CHECKIN)
            self.assertTrue(path.exists())
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.next_offset, 125)
            self.assertEqual(loaded.response_hashes, ("one", "two"))
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_access_restriction_signal_is_detected(self) -> None:
        self.assertTrue(
            response_has_access_restriction(
                {"errors": [{"message": "Verify you are human"}]}
            )
        )
        self.assertFalse(
            response_has_access_restriction(response_payload(75))
        )

    def test_access_restriction_does_not_retry_aggressively(self) -> None:
        context = FakeRequestContext(
            [FakeApiResponse(403, {"errors": [{"message": "access denied"}]})]
        )
        page = ReplayPage(context)
        collector = BookingPlaywrightCollector()
        options = CollectorOptions(screenshots_enabled=False)
        with self.assertRaises(AccessRestrictionError):
            collector._replay_booking_network_batch(
                page,
                request_url=REQUEST_URL,
                request_template=request_payload(),
                offset=100,
                options=options,
                log_callback=None,
            )
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(options.stats.get("network_batch_retries", 0), 0)

    def test_temporary_network_failure_uses_bounded_retry(self) -> None:
        context = FakeRequestContext(
            [
                FakeApiResponse(500, {"errors": [{"message": "temporary"}]}),
                FakeApiResponse(200, response_payload(100)),
            ]
        )
        page = ReplayPage(context)
        collector = BookingPlaywrightCollector()
        options = CollectorOptions(screenshots_enabled=False)
        batch, _size = collector._replay_booking_network_batch(
            page,
            request_url=REQUEST_URL,
            request_template=request_payload(),
            offset=100,
            options=options,
            log_callback=None,
        )
        self.assertEqual(batch.offset, 100)
        self.assertEqual(len(context.calls), 2)
        self.assertEqual(options.stats["network_batch_retries"], 1)
        self.assertEqual(page.waits, [1000, 900])

    def _run_collection(
        self,
        *,
        maximum: int = 150,
        options: CollectorOptions | None = None,
        replay_batches: dict[int, object] | None = None,
        fallback_status: str | None = None,
    ):
        collector = BookingPlaywrightCollector()
        page = FakePage()
        options = options or CollectorOptions(
            screenshots_enabled=False,
            disable_filters_during_complete_collection=True,
            resource_check_callback=lambda: ("ok", {"reason": "test"}),
        )
        if options.resource_check_callback is None:
            options.resource_check_callback = lambda: ("ok", {"reason": "test"})
        partial_events: list[dict] = []
        if options.partial_results_callback is None:
            options.partial_results_callback = (
                lambda _date, rows, metadata: partial_events.append(
                    {"rows": len(rows), **metadata}
                )
            )
        captured = parsed_batch(75)
        replay_batches = replay_batches or {
            100: parsed_batch(100),
            125: parsed_batch(125),
        }
        card_counts = iter((25, 75, 25))
        fallback_context = (
            patch.object(
                collector,
                "_bounded_dom_pagination_fallback",
                return_value=fallback_status,
            )
            if fallback_status is not None
            else nullcontext()
        )

        with (
            patch.object(
                collector,
                "_valid_card_count",
                side_effect=lambda *_args: next(card_counts, 25),
            ),
            patch.object(
                collector,
                "_scroll_to_bottom_until_load_more",
                return_value={"button": object()},
            ),
            patch.object(
                collector,
                "_bulk_extract_cards",
                return_value=[hotel(index) for index in range(75)],
            ),
            patch.object(
                collector,
                "_capture_booking_network_batch",
                return_value=(
                    captured,
                    REQUEST_URL,
                    request_payload(75),
                    1000,
                ),
            ),
            patch.object(
                collector,
                "_replay_booking_network_batch",
                side_effect=lambda _page, *, offset, **_kwargs: (
                    replay_batches[offset],
                    1000,
                ),
            ),
            patch.object(collector, "_wait_for_any_card_selector"),
            fallback_context,
        ):
            rows = collector._collect_network_batches(
                page=page,
                selector="div[data-testid='property-card']",
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
        return rows, options, partial_events, page

    def test_three_distinct_batches_are_bounded_and_persisted(self) -> None:
        rows, options, partial_events, page = self._run_collection()
        self.assertEqual(len(rows), 150)
        self.assertEqual(
            options.stats["network_batch_offsets"],
            [75, 100, 125],
        )
        self.assertEqual(
            options.stats["completion_status"],
            "completed_target_reached",
        )
        self.assertLessEqual(options.stats["maximum_live_dom_cards"], 75)
        self.assertGreaterEqual(len(partial_events), 5)
        self.assertEqual(partial_events[-1]["rows"], 150)
        self.assertEqual(len({row["hotel_url"] for row in rows}), 150)
        self.assertEqual(len(page.goto_calls), 1)

    def test_max_hotels_is_a_ceiling_for_network_batches(self) -> None:
        rows, options, partial_events, _page = self._run_collection(
            maximum=120,
        )
        self.assertEqual(len(rows), 120)
        self.assertEqual(partial_events[-1]["rows"], 120)
        self.assertTrue(
            all(int(event["rows"]) <= 120 for event in partial_events)
        )
        self.assertEqual(
            options.stats["completion_status"],
            "completed_target_reached",
        )
        self.assertLessEqual(
            len(options.stats["network_batch_offsets"]),
            2,
        )

    def test_server_pagination_proves_true_exhaustion(self) -> None:
        rows, options, _partial_events, _page = self._run_collection(
            replay_batches={100: parsed_batch(100, total=125)},
        )
        self.assertEqual(len(rows), 125)
        self.assertEqual(
            options.stats["completion_status"],
            "completed_results_exhausted",
        )
        self.assertTrue(options.stats["results_exhausted"])

    def test_repeated_replay_batch_finalizes_usable_records_as_plateau(self) -> None:
        repeated = parsed_batch(100)
        options = CollectorOptions(
            screenshots_enabled=False,
            disable_filters_during_complete_collection=True,
            resource_check_callback=lambda: ("ok", {"reason": "test"}),
        )
        rows, options, partial_events, _page = self._run_collection(
            options=options,
            replay_batches={100: repeated, 125: repeated},
            fallback_status="completed_pagination_plateau",
        )
        self.assertEqual(len(rows), 125)
        self.assertEqual(
            options.stats["completion_status"],
            "completed_pagination_plateau",
        )
        self.assertEqual(
            options.stats["network_batch_duplicate_fingerprints_ignored"],
            1,
        )
        self.assertEqual(partial_events[-1]["rows"], 125)

    def test_repeated_offset_with_resumable_partial_records_does_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            partial_dir = Path(directory)
            save_partial_records(
                partial_dir,
                CHECKIN,
                [hotel(index) for index in range(226)],
            )
            options = CollectorOptions(
                screenshots_enabled=False,
                disable_filters_during_complete_collection=True,
                resource_check_callback=lambda: ("ok", {"reason": "test"}),
                partial_dir=partial_dir,
                resume_partial_results=True,
            )
            rows, options, partial_events, _page = self._run_collection(
                maximum=250,
                options=options,
                fallback_status="completed_pagination_plateau",
            )
        self.assertEqual(len(rows), 226)
        self.assertEqual(
            options.stats["completion_status"],
            "completed_pagination_plateau",
        )
        self.assertEqual(
            options.stats["network_batch_repeat_diagnostic"]["offset"],
            75,
        )
        self.assertEqual(
            options.stats["network_batch_repeat_diagnostic"][
                "retained_unique_records"
            ],
            226,
        )
        self.assertEqual(partial_events[-1]["rows"], 226)

    def test_same_response_offset_with_new_identities_adds_rows(self) -> None:
        new_identities_same_offset = parse_booking_network_batch(
            response_payload(125),
            offset=100,
            rows_per_page=25,
        )
        rows, options, _partial_events, _page = self._run_collection(
            replay_batches={
                100: parsed_batch(100),
                125: new_identities_same_offset,
            },
        )
        self.assertEqual(len(rows), 150)
        self.assertEqual(options.stats["network_batch_offsets"], [75, 100, 100])
        self.assertEqual(
            options.stats["completion_status"],
            "completed_target_reached",
        )
        self.assertEqual(
            options.stats["network_batch_offset_mismatches"],
            [{"requested_offset": 125, "response_offset": 100}],
        )

    def test_partial_unique_records_reach_max_without_network_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            partial_dir = Path(directory)
            save_partial_records(
                partial_dir,
                CHECKIN,
                [hotel(index) for index in range(100)],
            )
            options = CollectorOptions(
                screenshots_enabled=False,
                disable_filters_during_complete_collection=True,
                resource_check_callback=lambda: ("ok", {"reason": "test"}),
                partial_dir=partial_dir,
                resume_partial_results=True,
            )
            rows, options, partial_events, page = self._run_collection(
                maximum=100,
                options=options,
            )
            continuation = load_continuation(partial_dir, CHECKIN)
        self.assertEqual(len(rows), 100)
        self.assertEqual(page.goto_calls, [])
        self.assertEqual(options.stats["network_batch_offsets"], [])
        self.assertEqual(
            options.stats["completion_status"],
            "completed_target_reached",
        )
        self.assertEqual(
            options.stats["network_batch_stop_reason"],
            "target_reached_from_partial",
        )
        self.assertEqual(partial_events[-1]["rows"], 100)
        self.assertIsNotNone(continuation)
        self.assertEqual(continuation.completion_status, "completed_target_reached")

    def test_collect_skips_chromium_when_partial_target_is_already_met(self) -> None:
        collector = BookingPlaywrightCollector()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_partial_records(
                root / "partial",
                CHECKIN,
                [hotel(index) for index in range(100)],
            )
            options = CollectorOptions(
                screenshots_enabled=False,
                disable_filters_during_complete_collection=True,
                partial_dir=root / "partial",
                resume_partial_results=True,
                screenshot_dir=root / "screenshots",
                debug_dir=root / "debug",
            )
            with patch(
                "collectors.booking_playwright.sync_playwright"
            ) as playwright:
                rows = collector.collect(
                    "Orlando",
                    CHECKIN,
                    CHECKOUT,
                    2,
                    "USD",
                    100,
                    False,
                    options,
                    progress_callback=None,
                    log_callback=None,
                )
        self.assertEqual(len(rows), 100)
        playwright.assert_not_called()
        self.assertTrue(options.stats["browser_launch_skipped_for_partial_target"])
        self.assertEqual(
            options.stats["completion_status"],
            "completed_target_reached",
        )

    def test_bounded_dom_fallback_plateaus_after_exactly_two_cycles(self) -> None:
        collector = BookingPlaywrightCollector()
        options = CollectorOptions(
            screenshots_enabled=False,
            disable_filters_during_complete_collection=True,
        )
        retained = PaginationResults([hotel(index) for index in range(25)])
        page = FakePage()
        with (
            patch.object(collector, "_valid_card_count", return_value=25),
            patch.object(
                collector,
                "_bulk_extract_cards",
                return_value=[hotel(index) for index in range(25)],
            ),
            patch.object(collector, "click_load_more_results", return_value=False) as click,
            patch.object(collector, "_bottom_visible_text", return_value=""),
        ):
            status = collector._bounded_dom_pagination_fallback(
                page=page,
                selector="cards",
                base_url=BASE_URL,
                city_or_region="Orlando",
                checkin_date=CHECKIN,
                checkout_date=CHECKOUT,
                number_of_nights=1,
                adults=2,
                currency="USD",
                maximum=100,
                retained=retained,
                options=options,
                filter_rows=lambda rows: rows,
                log_callback=None,
                screenshot_path=None,
                reason="duplicate_fingerprint",
            )
        self.assertEqual(status, "completed_pagination_plateau")
        self.assertEqual(click.call_count, 2)
        self.assertEqual(options.stats["network_dom_fallback_attempts"], 2)
        self.assertEqual(
            [row["new_unique"] for row in options.stats["network_dom_fallback_cycles"]],
            [0, 0],
        )

    def test_zero_usable_records_with_pagination_plateau_still_fails(self) -> None:
        collector = BookingPlaywrightCollector()
        options = CollectorOptions(
            screenshots_enabled=False,
            disable_filters_during_complete_collection=True,
        )
        with (
            patch.object(collector, "_valid_card_count", return_value=0),
            patch.object(collector, "_bulk_extract_cards", return_value=[]),
            patch.object(collector, "click_load_more_results", return_value=False),
            patch.object(collector, "_bottom_visible_text", return_value=""),
        ):
            with self.assertRaisesRegex(
                PaginationUnsupportedError,
                "zero usable",
            ):
                collector._bounded_dom_pagination_fallback(
                    page=FakePage(),
                    selector="cards",
                    base_url=BASE_URL,
                    city_or_region="Orlando",
                    checkin_date=CHECKIN,
                    checkout_date=CHECKOUT,
                    number_of_nights=1,
                    adults=2,
                    currency="USD",
                    maximum=100,
                    retained=PaginationResults(),
                    options=options,
                    filter_rows=lambda rows: rows,
                    log_callback=None,
                    screenshot_path=None,
                    reason="duplicate_fingerprint",
                )

    def test_status_callback_failure_does_not_discard_collected_rows(self) -> None:
        options = CollectorOptions(
            screenshots_enabled=False,
            disable_filters_during_complete_collection=True,
            resource_check_callback=lambda: ("ok", {"reason": "test"}),
            status_callback=lambda _payload: (_ for _ in ()).throw(
                OSError("status unavailable")
            ),
        )
        rows, options, _partial_events, _page = self._run_collection(
            options=options,
        )
        self.assertEqual(len(rows), 150)
        self.assertGreater(options.stats["status_callback_failure_count"], 0)
        self.assertEqual(
            options.stats["last_status_callback_error"],
            "status unavailable",
        )

    def test_resource_stop_persists_bootstrap_and_remains_incomplete(self) -> None:
        calls = 0

        def resource():
            nonlocal calls
            calls += 1
            return "soft", {"reason": "test pressure", "available_ram_mb": 800}

        options = CollectorOptions(
            screenshots_enabled=False,
            disable_filters_during_complete_collection=True,
            resource_check_callback=resource,
        )
        partial_events: list[tuple[int, str]] = []
        options.partial_results_callback = (
            lambda _date, rows, metadata: partial_events.append(
                (len(rows), metadata["completion_status"])
            )
        )
        with self.assertRaises(ResourceLimitExceeded):
            self._run_collection(options=options)
        self.assertGreaterEqual(calls, 1)
        self.assertTrue(partial_events)
        self.assertEqual(partial_events[-1][1], "incomplete_resource_soft_limit")
        self.assertEqual(
            options.stats["completion_status"],
            "incomplete_resource_soft_limit",
        )

    def test_resume_skips_saved_batches_and_adds_missing_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            partial_dir = Path(directory)
            initial = [hotel(index) for index in range(125)]
            save_partial_records(partial_dir, CHECKIN, initial)
            hash_75 = parsed_batch(75).response_hash
            hash_100 = parsed_batch(100).response_hash
            save_continuation(
                partial_dir,
                CHECKIN,
                next_offset=125,
                rows_per_page=25,
                response_hashes=(hash_75, hash_100),
                unique_records=125,
                completion_status="incomplete_network_batches",
            )
            options = CollectorOptions(
                screenshots_enabled=False,
                disable_filters_during_complete_collection=True,
                resource_check_callback=lambda: ("ok", {"reason": "test"}),
                partial_dir=partial_dir,
                resume_partial_results=True,
            )
            rows, options, _partials, _page = self._run_collection(
                options=options,
                replay_batches={125: parsed_batch(125)},
            )
        self.assertEqual(len(rows), 150)
        self.assertEqual(options.stats["network_batch_offsets"], [125])
        self.assertEqual(
            options.stats["completion_status"],
            "completed_target_reached",
        )

    def test_only_proven_completed_dates_are_skipped(self) -> None:
        checkpoint = {
            "date_statuses": {
                "2026-09-10": {"status": "completed_target_reached"},
                "2026-09-11": {"status": "incomplete_network_request_unavailable"},
                "2026-09-12": {"status": "completed_results_exhausted"},
                "2026-09-13": {"status": "incomplete_repeated_network_batch"},
            }
        }
        completed = {
            key for key, value in checkpoint["date_statuses"].items()
            if value.get("status") in {"completed_target_reached", "completed_results_exhausted"}
        }
        self.assertEqual(completed, {"2026-09-10", "2026-09-12"})

    def test_sqlite_writes_remain_idempotent(self) -> None:
        import database.db as database

        with tempfile.TemporaryDirectory() as directory:
            test_db = Path(directory) / "network.sqlite"
            original = database.SQLITE_DB_PATH
            try:
                database.SQLITE_DB_PATH = test_db
                database.init_db("sqlite")
                rows = PaginationResults([hotel(index) for index in range(25)]).values()
                for row in rows:
                    row["collection_run_id"] = 9901
                database.insert_hotel_results(rows, backend="sqlite")
                database.insert_hotel_results(rows, backend="sqlite")
                with sqlite3.connect(test_db) as connection:
                    count = connection.execute(
                        "select count(*) from hotel_price_results "
                        "where collection_run_id = 9901"
                    ).fetchone()[0]
                self.assertEqual(count, 25)
            finally:
                database.SQLITE_DB_PATH = original


if __name__ == "__main__":
    unittest.main()
