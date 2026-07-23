from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from unittest.mock import patch
from services.resource_guard import ResourceGuard, ResourceLimitExceeded, ScraperAlreadyRunning, SingleScraperLock, ResourceSnapshot, ResourceThresholds, evaluate_snapshot


def snapshot(**updates):
    values = {
        "timestamp": "2026-07-21 12:00:00",
        "owner_pid": 123,
        "available_ram_mb": 2000.0,
        "swap_used_mb": 100.0,
        "swap_free_mb": 2000.0,
        "swap_percent": 5.0,
        "python_rss_mb": 200.0,
        "browser_rss_mb": 500.0,
        "browser_process_count": 4,
        "disk_free_gb": 100.0,
    }
    values.update(updates)
    return ResourceSnapshot(**values)


class ResourceGuardTests(unittest.TestCase):
    def setUp(self):
        self.thresholds = ResourceThresholds()

    def test_refuses_start_below_800_mb(self):
        level, reason = evaluate_snapshot(snapshot(available_ram_mb=700), self.thresholds, starting=True)
        self.assertEqual(level, "stop")
        self.assertIn("start threshold", reason)

    def test_emergency_below_500_mb(self):
        level, _ = evaluate_snapshot(snapshot(available_ram_mb=499), self.thresholds)
        self.assertEqual(level, "emergency")

    def test_high_swap_with_safe_available_ram_is_warning(self):
        level, reason = evaluate_snapshot(snapshot(available_ram_mb=2386.9, swap_used_mb=3876.4, swap_free_mb=219.6, swap_percent=94.6, python_rss_mb=101.9, browser_rss_mb=1885.5), self.thresholds)
        self.assertEqual(level, "warning")
        self.assertIn("94.6%", reason)
        self.assertIn("220 MB free", reason)
        self.assertIn("2387 MB", reason)

    def test_high_swap_with_low_available_ram_is_emergency(self):
        level, _ = evaluate_snapshot(snapshot(available_ram_mb=800, swap_free_mb=200, swap_percent=95), self.thresholds)
        self.assertEqual(level, "emergency")

    def test_browser_rss_stop(self):
        level, _ = evaluate_snapshot(snapshot(browser_rss_mb=2700), self.thresholds)
        self.assertEqual(level, "soft_recovery")

    def test_browser_pss_avoids_summed_rss_double_count(self):
        level, _ = evaluate_snapshot(
            snapshot(browser_rss_mb=3000, browser_pss_mb=1700),
            self.thresholds,
        )
        self.assertEqual(level, "ok")

    def test_swap_pressure_between_hard_floor_and_recovery_is_soft(self):
        level, _ = evaluate_snapshot(
            snapshot(available_ram_mb=922, swap_free_mb=208, swap_percent=94.9),
            self.thresholds,
        )
        self.assertEqual(level, "soft_recovery")

    def test_python_rss_stop(self):
        level, _ = evaluate_snapshot(snapshot(python_rss_mb=2500), self.thresholds)
        self.assertEqual(level, "stop")

    def test_low_disk_emergency(self):
        level, _ = evaluate_snapshot(snapshot(disk_free_gb=4), self.thresholds)
        self.assertEqual(level, "emergency")

    def test_guard_does_not_raise_warning_but_raises_emergency(self):
        guard = ResourceGuard(".", thresholds=self.thresholds, minimum_check_interval_seconds=0)
        safe_swap = snapshot(available_ram_mb=2386.9, swap_free_mb=219.6, swap_percent=94.6)
        with patch("services.resource_guard.resource_snapshot", return_value=safe_swap):
            self.assertEqual(guard.check(force=True), safe_swap)
            self.assertEqual(guard.last_level, "warning")
        with patch("services.resource_guard.resource_snapshot", return_value=snapshot(available_ram_mb=700, swap_free_mb=200, swap_percent=95)):
            with self.assertRaises(ResourceLimitExceeded):
                guard.check(force=True)

    def test_normal_snapshot(self):
        level, _ = evaluate_snapshot(snapshot(), self.thresholds)
        self.assertEqual(level, "ok")

    def test_host_lock_refuses_second_scraper(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "active.lock"
            first = SingleScraperLock(lock_path)
            second = SingleScraperLock(lock_path)
            first.acquire("first-job")
            try:
                with self.assertRaises(ScraperAlreadyRunning):
                    second.acquire("second-job")
            finally:
                first.release()


if __name__ == "__main__":
    unittest.main()
