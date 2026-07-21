from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from services.resource_guard import ScraperAlreadyRunning, SingleScraperLock, ResourceSnapshot, ResourceThresholds, evaluate_snapshot


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

    def test_emergency_before_swap_exhaustion(self):
        level, _ = evaluate_snapshot(snapshot(swap_free_mb=200, swap_percent=92), self.thresholds)
        self.assertEqual(level, "emergency")

    def test_browser_rss_stop(self):
        level, _ = evaluate_snapshot(snapshot(browser_rss_mb=2700), self.thresholds)
        self.assertEqual(level, "stop")

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
