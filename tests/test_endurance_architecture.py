from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app import date_attempt_profile_dir
from collectors.base import CollectorOptions
from collectors.booking_playwright import BookingPlaywrightCollector
from services.endurance import (
    IncrementalResultBuffer,
    canonical_hotel_key,
    cleanup_ephemeral_profile,
    prepare_ephemeral_profile,
)
from services.job_runner import CancellationSignal
from services.playwright_safe import find_owned_browser_processes
from services.resource_guard import ResourceSnapshot, ResourceThresholds, evaluate_snapshot


def raw_card(index: int, *, hotel_url: str | None = None) -> dict:
    return {
        "raw_hotel_name": f"Hotel {index}",
        "hotel_url": hotel_url or f"https://www.booking.com/hotel/us/hotel-{index}.html?aid=1",
        "review_score": "8.5",
        "review_count": "100 reviews",
        "cheapest_room_name": "Room",
        "raw_price_text": f"US$ {100 + index}",
        "cheapest_price_total": f"US$ {100 + index}",
        "raw_card_text": f"Hotel {index} US$ {100 + index}",
        "star_rating": 3,
        "raw_star_signal": "3 stars",
        "star_aria_label": "3 stars",
        "star_icon_count": 3,
        "star_rating_missing_reason": None,
    }


class FakeLocator:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def count(self) -> int:
        return len(self.rows)

    def evaluate_all(self, script: str, argument=None):
        if "removeCount" in script:
            removed = min(int(argument), len(self.rows))
            del self.rows[:removed]
            return removed
        if isinstance(argument, dict) and {"start", "end"} <= set(argument):
            return [dict(row) for row in self.rows[argument["start"] : argument["end"]]]
        raise AssertionError(f"unexpected evaluate_all call: {argument!r}")


class FakePage:
    def __init__(self, rows: list[dict]) -> None:
        self.cards = FakeLocator(rows)
        self.url = "https://www.booking.com/searchresults.html"

    def locator(self, _selector: str) -> FakeLocator:
        return self.cards

    def evaluate(self, _script: str):
        return 1000


class FakeEvent:
    def __init__(self) -> None:
        self.value = False

    def clear(self) -> None:
        self.value = False

    def set(self) -> None:
        self.value = True

    def is_set(self) -> bool:
        return self.value


class FakeProcess:
    def __init__(self, pid: int, name: str, command: list[str]) -> None:
        self.pid = pid
        self.info = {"pid": pid, "name": name, "cmdline": command}


class EnduranceArchitectureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.collector = BookingPlaywrightCollector()
        self.collector._incremental_results = IncrementalResultBuffer(500)
        self.collector._incremental_processed_dom = 0
        self.collector._incremental_rank_offset = 0
        self.collector._incremental_context = {
            "city_or_region": "Orlando",
            "search_url": "https://www.booking.com/searchresults.html",
            "checkin_date": date(2026, 8, 1),
            "checkout_date": date(2026, 8, 2),
            "number_of_nights": 1,
            "adults": 2,
            "currency": "USD",
        }
        self.options = CollectorOptions(screenshots_enabled=False)

    def test_incremental_batches_extract_only_new_cards_and_dedupe(self):
        page = FakePage([raw_card(index) for index in range(25)])
        partial_counts: list[int] = []
        self.options.partial_results_callback = (
            lambda _date, records, _metadata: partial_counts.append(len(records))
        )
        self.assertEqual(
            self.collector._capture_incremental_cards(page, "card", self.options, None),
            25,
        )
        page.cards.rows.extend(
            [raw_card(0, hotel_url="https://www.booking.com/hotel/us/hotel-0.html?different=1")]
            + [raw_card(index) for index in range(25, 49)]
        )
        self.assertEqual(
            self.collector._capture_incremental_cards(page, "card", self.options, None),
            24,
        )
        self.assertEqual(len(self.collector._incremental_results), 49)
        self.assertEqual(partial_counts, [25, 49])
        self.assertGreaterEqual(self.collector._incremental_results.duplicates_seen, 1)

    def test_dom_pruning_retains_records_and_allows_next_batch(self):
        page = FakePage([raw_card(index) for index in range(125)])
        self.collector._capture_incremental_cards(page, "card", self.options, None)
        with patch.dict(
            "os.environ",
            {
                "OTA_BOUND_BOOKING_DOM": "1",
                "OTA_MAX_LIVE_BOOKING_CARDS": "100",
                "OTA_KEEP_LIVE_BOOKING_CARDS": "25",
            },
        ):
            remaining = self.collector._prune_extracted_dom_cards(
                page, "card", self.options, None
            )
        self.assertEqual(remaining, 25)
        self.assertEqual(len(self.collector._incremental_results), 125)
        page.cards.rows.extend(raw_card(index) for index in range(125, 150))
        self.assertEqual(
            self.collector._capture_incremental_cards(page, "card", self.options, None),
            25,
        )
        self.assertEqual(len(self.collector._incremental_results), 150)

    def test_buffer_is_bounded_and_canonical_url_ignores_query(self):
        buffer = IncrementalResultBuffer(3)
        rows = [
            {"hotel_url": f"https://www.booking.com/hotel/us/{index}.html?aid=1"}
            for index in range(10)
        ]
        buffer.add(rows)
        self.assertEqual(len(buffer), 3)
        self.assertEqual(
            canonical_hotel_key({"hotel_url": "https://x.test/a?one=1"}),
            canonical_hotel_key({"hotel_url": "https://x.test/a?two=2"}),
        )

    def test_profile_is_unique_per_date_and_attempt(self):
        first = date_attempt_profile_dir("job", False, date(2026, 8, 1), 1)
        next_date = date_attempt_profile_dir("job", False, date(2026, 8, 2), 1)
        retry = date_attempt_profile_dir("job", False, date(2026, 8, 1), 2)
        self.assertEqual(len({first, next_date, retry}), 3)

    def test_only_marked_attempt_profile_is_removed_after_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = prepare_ephemeral_profile(
                root / "jobs" / "job" / "visible" / "2026-08-01" / "attempt_1"
            )
            (attempt / "Cache").mkdir()
            (attempt / "Cache" / "entry").write_bytes(b"x" * 1024)
            unmarked = root / "attempt_2"
            unmarked.mkdir()

            self.assertTrue(cleanup_ephemeral_profile(attempt))
            self.assertFalse(attempt.exists())
            self.assertFalse(cleanup_ephemeral_profile(unmarked))
            self.assertTrue(unmarked.exists())

    def test_resource_levels_distinguish_soft_and_hard(self):
        base = dict(
            timestamp="now",
            owner_pid=1,
            swap_used_mb=3800,
            swap_free_mb=200,
            swap_percent=95,
            python_rss_mb=100,
            browser_rss_mb=1800,
            browser_process_count=10,
            disk_free_gb=100,
        )
        soft = ResourceSnapshot(available_ram_mb=950, **base)
        hard = ResourceSnapshot(available_ram_mb=500, **base)
        self.assertEqual(evaluate_snapshot(soft, ResourceThresholds())[0], "soft_recovery")
        self.assertEqual(evaluate_snapshot(hard, ResourceThresholds())[0], "emergency")

    def test_soft_resource_pressure_finishes_incremental_extraction(self):
        page = FakePage([raw_card(index) for index in range(25)])
        self.options.stats["resource_guard_level"] = "soft_recovery"
        with patch.object(
            self.collector,
            "_extract_booking_visible_result_count",
            return_value=200,
        ), patch.object(
            self.collector,
            "_best_hotel_card_count",
            return_value=25,
        ), patch.object(
            self.collector,
            "_update_unique_hotels_from_page",
            return_value=0,
        ):
            loaded = self.collector.load_all_booking_results(
                page,
                max_hotels=500,
                collect_all_available_hotels=False,
                max_scroll_minutes=5,
                selector="card",
                options=self.options,
            )

        self.assertEqual(loaded, 25)
        self.assertEqual(
            self.options.stats["completion_status"],
            "completed_resource_safe_limit",
        )
        self.assertEqual(len(self.collector._incremental_results), 25)

    def test_fresh_cancellation_signal_clears_prior_resource_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cancel.json"
            first = CancellationSignal(FakeEvent(), path, "job")
            first.set("resource_limit")
            resumed = CancellationSignal(FakeEvent(), path, "job")
            resumed.reset()
            self.assertFalse(resumed.is_set())

    def test_reparented_owned_chromium_is_found_but_personal_chrome_is_not(self):
        profile = "/tmp/ota-job-profile"
        descendant = FakeProcess(11, "chromium", ["chromium", f"--user-data-dir={profile}"])
        reparented = FakeProcess(12, "chromium", ["chromium", f"--user-data-dir={profile}"])
        personal = FakeProcess(
            13,
            "chrome",
            ["chrome", "--user-data-dir=/home/jf/.config/google-chrome"],
        )
        owner = type("Owner", (), {"children": lambda self, recursive=True: [descendant]})()
        with patch("services.playwright_safe.psutil.Process", return_value=owner), patch(
            "services.playwright_safe.psutil.process_iter",
            return_value=[reparented, personal],
        ):
            found = find_owned_browser_processes(99, profile_dir=profile)
        self.assertEqual({process.pid for process in found}, {11, 12})


if __name__ == "__main__":
    unittest.main()
