from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app import (
    final_status_metrics,
    should_show_stale_heartbeat_warning,
    validate_booking_records_for_persistence,
)
from collectors.base import CollectorOptions
from collectors.booking_playwright import BookingPlaywrightCollector
from services.booking_date_integrity import (
    canonical_search_url,
    evaluate_date_integrity,
    query_date_values,
)
from services.booking_pagination import PaginationResults
from services.hotel_classifier import classify_booking_property
from services.job_runner import save_partial_records
from services.partial_policy import (
    POLICY_DISABLED,
    POLICY_EXPLICIT,
    POLICY_SAME_JOB,
    build_partial_metadata,
    partial_load_decision,
    write_partial_metadata,
)


CHECKIN = date(2026, 8, 31)
CHECKOUT = date(2026, 9, 1)
SEARCH_URL = (
    "https://www.booking.com/searchresults.html?ss=Orlando"
    "&checkin=2026-08-31&checkout=2026-09-01"
)


def hotel(index: int) -> dict:
    return {
        "hotel_name": f"Hotel {index}",
        "hotel_url": f"https://www.booking.com/hotel/us/hotel-{index}.html",
        "collection_status": "success",
        "checkin_date": CHECKIN,
        "checkout_date": CHECKOUT,
    }


def fingerprint(**updates) -> dict:
    value = {
        "instance_id": "integrity_test",
        "source": "Booking.com",
        "city_or_destination_id": "orlando",
        "checkin": CHECKIN.isoformat(),
        "checkout": CHECKOUT.isoformat(),
        "stay_length": 1,
        "adults": 2,
        "rooms": 1,
        "currency": "USD",
        "selected_star_ratings": [1, 2, 3, 4, 5],
        "include_unknown_star_rating": True,
        "hotels_only": True,
        "collect_all": False,
        "maximum_hotels": 250,
        "sort_order": "price",
    }
    value.update(updates)
    return value


class DateIntegrityTests(unittest.TestCase):
    def test_requested_and_effective_dates_match(self) -> None:
        report = evaluate_date_integrity(
            requested_checkin=CHECKIN,
            requested_checkout=CHECKOUT,
            page_url=SEARCH_URL,
            collector_checkin=CHECKIN,
            collector_checkout=CHECKOUT,
        )
        self.assertTrue(report.date_integrity_verified)
        self.assertEqual(report.effective_checkin_date, "2026-08-31")
        self.assertEqual(report.effective_checkout_date, "2026-09-01")

    def test_one_day_shift_is_detected(self) -> None:
        report = evaluate_date_integrity(
            requested_checkin=CHECKIN,
            requested_checkout=CHECKOUT,
            page_url=(
                "https://www.booking.com/searchresults.html?"
                "checkin=2026-09-01&checkout=2026-09-02"
            ),
        )
        self.assertFalse(report.date_integrity_verified)
        self.assertIn(
            "effective_dates_do_not_match_requested",
            report.mismatch_reasons,
        )

    def test_conflicting_duplicate_query_parameters_are_rejected(self) -> None:
        url = (
            f"{SEARCH_URL}&checkin=2026-09-01&checkout=2026-09-02"
        )
        self.assertEqual(
            query_date_values(url)["checkin"],
            ["2026-08-31", "2026-09-01"],
        )
        report = evaluate_date_integrity(
            requested_checkin=CHECKIN,
            requested_checkout=CHECKOUT,
            page_url=url,
        )
        self.assertFalse(report.date_integrity_verified)
        self.assertIn(
            "conflicting_duplicate_url_dates",
            report.mismatch_reasons,
        )

    def test_stale_telemetry_url_is_not_integrity_evidence(self) -> None:
        report = evaluate_date_integrity(
            requested_checkin=CHECKIN,
            requested_checkout=CHECKOUT,
            page_url=SEARCH_URL,
            telemetry_url=(
                "https://www.booking.com/searchresults.html?"
                "checkin=2026-09-01&checkout=2026-09-02"
            ),
        )
        self.assertTrue(report.date_integrity_verified)

    def test_visible_date_picker_mismatch_is_rejected(self) -> None:
        report = evaluate_date_integrity(
            requested_checkin=CHECKIN,
            requested_checkout=CHECKOUT,
            page_url=SEARCH_URL,
            visible_checkin_values=["2026-09-01"],
            visible_checkout_values=["2026-09-02"],
        )
        self.assertFalse(report.date_integrity_verified)
        self.assertIn(
            "visible_checkin_does_not_match_requested",
            report.mismatch_reasons,
        )

    def test_canonical_url_removes_date_drift_and_duplicates(self) -> None:
        repaired = canonical_search_url(
            (
                f"{SEARCH_URL}&checkin=2026-09-01"
                "&checkout=2026-09-02"
            ),
            requested_checkin=CHECKIN,
            requested_checkout=CHECKOUT,
        )
        self.assertEqual(query_date_values(repaired)["checkin"], ["2026-08-31"])
        self.assertEqual(query_date_values(repaired)["checkout"], ["2026-09-01"])

    def test_one_canonical_reload_repairs_live_page_drift(self) -> None:
        collector = BookingPlaywrightCollector()

        class Page:
            def __init__(self):
                self.goto_calls: list[str] = []

            def goto(self, url, **_kwargs):
                self.goto_calls.append(url)

        page = Page()
        sources = iter(
            (
                {
                    "page_url": (
                        "https://www.booking.com/searchresults.html?"
                        "checkin=2026-09-01&checkout=2026-09-02"
                    ),
                    "visible_checkin": [],
                    "visible_checkout": [],
                    "hidden_checkin": [],
                    "hidden_checkout": [],
                },
                {
                    "page_url": SEARCH_URL,
                    "visible_checkin": [],
                    "visible_checkout": [],
                    "hidden_checkin": ["2026-08-31"],
                    "hidden_checkout": ["2026-09-01"],
                },
            )
        )
        options = CollectorOptions()
        with (
            patch.object(
                collector,
                "_inspect_date_sources",
                side_effect=lambda _page: next(sources),
            ),
            patch.object(collector, "_wait_for_results_page"),
            patch(
                "collectors.booking_playwright.interruptible_page_wait",
                return_value=True,
            ),
        ):
            report = collector._ensure_date_integrity(
                page,
                requested_checkin=CHECKIN,
                requested_checkout=CHECKOUT,
                canonical_url=SEARCH_URL,
                options=options,
                log_callback=None,
                allow_corrective_reload=True,
            )
        self.assertTrue(report.date_integrity_verified)
        self.assertEqual(len(page.goto_calls), 1)
        self.assertEqual(options.stats["date_integrity_corrective_reloads"], 1)

    def test_mismatched_records_are_refused_before_persistence(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "date_integrity_mismatch"):
            validate_booking_records_for_persistence(
                [
                    {
                        **hotel(1),
                        "date_integrity_verified": True,
                        "effective_checkin_date": date(2026, 9, 1),
                        "effective_checkout_date": date(2026, 9, 2),
                    }
                ],
                expected_checkin=CHECKIN,
                expected_checkout=CHECKOUT,
            )


class PartialPolicyTests(unittest.TestCase):
    def test_fresh_resume_disabled_rejects_existing_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_partial_records(root, CHECKIN, [hotel(1)])
            write_partial_metadata(
                root,
                CHECKIN,
                build_partial_metadata(
                    fingerprint(), job_id="old", run_id=1
                ),
            )
            messages: list[str] = []
            rows = BookingPlaywrightCollector()._load_resumable_partial(
                CHECKIN,
                CollectorOptions(
                    partial_dir=root,
                    resume_partial_results=False,
                    partial_resume_policy=POLICY_DISABLED,
                ),
                messages.append,
            )
        self.assertEqual(rows, [])
        self.assertIn("Partial rejected: resume disabled", messages)

    def test_explicit_resume_accepts_matching_partial(self) -> None:
        metadata = build_partial_metadata(
            fingerprint(), job_id="old", run_id=1
        )
        decision = partial_load_decision(
            policy=POLICY_EXPLICIT,
            expected_fingerprint=fingerprint(),
            metadata=metadata,
            current_job_id="new",
            current_run_id=2,
        )
        self.assertTrue(decision.allowed)

    def test_same_job_crash_recovery_requires_same_job_and_run(self) -> None:
        metadata = build_partial_metadata(
            fingerprint(), job_id="same", run_id=7
        )
        accepted = partial_load_decision(
            policy=POLICY_SAME_JOB,
            expected_fingerprint=fingerprint(),
            metadata=metadata,
            current_job_id="same",
            current_run_id=7,
        )
        rejected = partial_load_decision(
            policy=POLICY_SAME_JOB,
            expected_fingerprint=fingerprint(),
            metadata=metadata,
            current_job_id="other",
            current_run_id=7,
        )
        self.assertTrue(accepted.allowed)
        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.log_message, "Partial rejected: job mismatch")

    def test_configuration_mismatches_are_rejected(self) -> None:
        original = fingerprint()
        metadata = build_partial_metadata(original, job_id="old", run_id=1)
        for changed in (
            {"checkin": "2026-09-01", "checkout": "2026-09-02"},
            {"adults": 3},
            {"currency": "EUR"},
            {"hotels_only": False},
            {"selected_star_ratings": [3, 4, 5]},
            {"maximum_hotels": 100},
        ):
            with self.subTest(changed=changed):
                decision = partial_load_decision(
                    policy=POLICY_EXPLICIT,
                    expected_fingerprint=fingerprint(**changed),
                    metadata=metadata,
                    current_job_id="new",
                    current_run_id=2,
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(
                    decision.log_message,
                    "Partial rejected: configuration mismatch",
                )


class HotelClassifierTests(unittest.TestCase):
    def test_private_rooms_and_vacation_homes_are_excluded(self) -> None:
        for name in (
            "Winsor Hills Resort Private Room and Bath by Disney World",
            "Pixie hollow single vacation retreat resort dwelling",
            "Entire apartment near Disney",
            "Affordable Suite with Bath near Disney World and SeaWorld",
            "Lyrios Vacation Rentals - The Palms",
        ):
            with self.subTest(name=name):
                result = classify_booking_property(hotel_name=name)
                self.assertFalse(result.is_hotel_eligible)

    def test_legitimate_hotels_remain_eligible(self) -> None:
        for name in (
            "Residence Inn Orlando",
            "Holiday Inn Resort Orlando Suites",
            "Extended Stay America Suites",
            "Lake Buena Vista Resort Hotel",
            "Roadside Motel and Lodge",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    classify_booking_property(
                        hotel_name=name
                    ).is_hotel_eligible
                )

    def test_structured_rental_metadata_wins_over_resort_name(self) -> None:
        dom = classify_booking_property(
            hotel_name="Sunshine Resort",
            structured_metadata={"accommodation_type": "Private room"},
        )
        network = classify_booking_property(
            hotel_name="Sunshine Resort",
            structured_metadata={"accommodation_type": {"name": "Private room"}},
        )
        self.assertFalse(dom.is_hotel_eligible)
        self.assertFalse(network.is_hotel_eligible)


class _Page:
    def evaluate(self, _script):
        return 1000


class AdaptivePaginationTests(unittest.TestCase):
    def test_continues_while_load_more_adds_cards_and_stops_at_exact_target(self):
        collector = BookingPlaywrightCollector()
        retained = PaginationResults([hotel(index) for index in range(25)])
        options = CollectorOptions(max_scroll_minutes=5)
        counts = iter((25, 25, 50, 50, 75, 75, 100))

        def extract(*args, **kwargs):
            end = int(args[2])
            start = int(kwargs.get("start_card") or 0)
            return [hotel(index) for index in range(start, end)]

        with (
            patch.object(
                collector,
                "_valid_card_count",
                side_effect=lambda *_args: next(counts),
            ),
            patch.object(collector, "_bulk_extract_cards", side_effect=extract),
            patch.object(
                collector, "click_load_more_results", return_value=True
            ) as click,
            patch.object(collector, "_bottom_visible_text", return_value=""),
        ):
            status = collector._bounded_dom_pagination_fallback(
                page=_Page(),
                selector="cards",
                base_url=SEARCH_URL,
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
                reason="network_no_growth",
            )
        self.assertEqual(status, "completed_target_reached")
        self.assertEqual(retained.successful_count(), 100)
        self.assertEqual(click.call_count, 3)
        self.assertEqual(options.stats["consecutive_dom_no_growth"], 0)

    def test_verified_end_of_results_stops_below_target(self) -> None:
        collector = BookingPlaywrightCollector()
        retained = PaginationResults([hotel(index) for index in range(10)])
        with (
            patch.object(collector, "_valid_card_count", return_value=10),
            patch.object(
                collector,
                "_bulk_extract_cards",
                return_value=[hotel(index) for index in range(10)],
            ),
            patch.object(
                collector,
                "_bottom_visible_text",
                return_value="You have reached the end of the list",
            ),
        ):
            status = collector._bounded_dom_pagination_fallback(
                page=_Page(),
                selector="cards",
                base_url=SEARCH_URL,
                city_or_region="Orlando",
                checkin_date=CHECKIN,
                checkout_date=CHECKOUT,
                number_of_nights=1,
                adults=2,
                currency="USD",
                maximum=250,
                retained=retained,
                options=CollectorOptions(),
                filter_rows=lambda rows: rows,
                log_callback=None,
                screenshot_path=None,
                reason="network_no_growth",
            )
        self.assertEqual(status, "completed_verified_end_of_results")

    def test_max_scroll_time_is_a_success_when_usable_rows_exist(self) -> None:
        collector = BookingPlaywrightCollector()
        options = CollectorOptions(max_scroll_minutes=1)
        options.stats["pagination_started_monotonic"] = 100.0
        with (
            patch.object(collector, "_valid_card_count", return_value=1),
            patch(
                "collectors.booking_playwright.time.perf_counter",
                return_value=161.0,
            ),
        ):
            status = collector._bounded_dom_pagination_fallback(
                page=_Page(),
                selector="cards",
                base_url=SEARCH_URL,
                city_or_region="Orlando",
                checkin_date=CHECKIN,
                checkout_date=CHECKOUT,
                number_of_nights=1,
                adults=2,
                currency="USD",
                maximum=250,
                retained=PaginationResults([hotel(1)]),
                options=options,
                filter_rows=lambda rows: rows,
                log_callback=None,
                screenshot_path=None,
                reason="network_no_growth",
            )
        self.assertEqual(status, "completed_max_scroll_time_with_results")
        self.assertTrue(options.stats["maximum_scroll_time_reached"])

    def test_identity_collision_is_reported(self) -> None:
        retained = PaginationResults(
            [
                {
                    **hotel(1),
                    "hotel_name": "First Property",
                }
            ]
        )
        diagnostic = retained.add_with_diagnostics(
            [
                {
                    **hotel(1),
                    "hotel_name": "Different Property",
                }
            ]
        )
        self.assertEqual(diagnostic["new_identities"], 0)
        self.assertEqual(diagnostic["identity_collisions"], 1)


class DashboardSemanticsTests(unittest.TestCase):
    def test_terminal_status_suppresses_stale_heartbeat_warning(self) -> None:
        for status in (
            "completed_all_dates",
            "completed_with_failed_dates",
            "stopped",
            "failed",
        ):
            self.assertFalse(
                should_show_stale_heartbeat_warning(status, True)
            )
        self.assertTrue(
            should_show_stale_heartbeat_warning("running", True)
        )

    def test_full_database_aggregates_are_not_capped_at_preview_limit(self):
        import database.db as database

        with tempfile.TemporaryDirectory() as directory:
            original = database.SQLITE_DB_PATH
            database.SQLITE_DB_PATH = Path(directory) / "aggregate.sqlite"
            try:
                database.init_db("sqlite")
                run_id = database.create_collection_run(
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
                rows = []
                for index in range(550):
                    rows.append(
                        {
                            **hotel(index),
                            "collection_run_id": run_id,
                            "source": "Booking.com",
                            "city_or_region": "Orlando",
                            "raw_price_text": f"US${100 + index}",
                            "parsed_price": 100 + index,
                            "cheapest_price_total": 100 + index,
                            "date_integrity_verified": True,
                            "requested_checkin_date": CHECKIN,
                            "requested_checkout_date": CHECKOUT,
                            "effective_checkin_date": CHECKIN,
                            "effective_checkout_date": CHECKOUT,
                        }
                    )
                database.insert_hotel_results(rows, backend="sqlite")
                preview = database.fetch_results_by_run_id_limited(
                    run_id, 500, backend="sqlite"
                )
                aggregate = database.result_aggregates_by_run_id(
                    run_id, backend="sqlite"
                )
            finally:
                database.SQLITE_DB_PATH = original
        self.assertEqual(len(preview), 500)
        self.assertEqual(aggregate["total_observations"], 550)
        self.assertEqual(aggregate["rows_with_raw_price"], 550)
        self.assertEqual(aggregate["rows_with_parsed_price"], 550)

    def test_final_status_uses_one_based_final_date_index_and_full_counts(self):
        metrics = final_status_metrics(
            planned_dates=[
                date(2026, 8, 30),
                date(2026, 8, 31),
            ],
            number_of_nights=1,
            date_status_rows=[
                {"status": "completed_target_reached"},
                {"status": "completed_verified_plateau"},
            ],
            aggregates={
                "total_observations": 490,
                "unique_hotels": 260,
                "rows_with_raw_price": 490,
                "rows_with_parsed_price": 489,
                "rows_missing_price": 1,
            },
            preview_rows=500,
        )
        self.assertEqual(metrics["current_stay_index"], 2)
        self.assertEqual(metrics["completed_stay_dates"], 2)
        self.assertEqual(metrics["current_checkin_date"], "2026-08-31")
        self.assertEqual(metrics["current_checkout_date"], "2026-09-01")
        self.assertEqual(metrics["total_observations"], 490)
        self.assertEqual(metrics["estimated_remaining_time"], "Completed")


if __name__ == "__main__":
    unittest.main()
