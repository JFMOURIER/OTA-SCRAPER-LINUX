from __future__ import annotations

import unittest
from datetime import date

from app import completed_dates_from_checkpoint_and_db
from services.job_runner import build_checkpoint, update_checkpoint_date


class ResumeCompletionSemanticsTests(unittest.TestCase):
    def _checkpoint(self, status: str | None = None, *, listed: bool = False):
        checkpoint = {
            "completed_dates": ["2026-09-10"] if listed else [],
            "date_statuses": {},
        }
        if status is not None:
            checkpoint["date_statuses"]["2026-09-10"] = {
                "status": status,
                "records_collected": 99,
            }
        return checkpoint

    def test_resource_safe_limit_listed_in_completed_dates_is_not_skipped(self):
        checkpoint = self._checkpoint("completed_resource_safe_limit", listed=True)
        self.assertEqual(completed_dates_from_checkpoint_and_db(checkpoint, {"2026-09-10": 99}), set())

    def test_hard_safety_limit_is_not_skipped(self):
        checkpoint = self._checkpoint("completed_hard_safety_limit", listed=True)
        self.assertEqual(completed_dates_from_checkpoint_and_db(checkpoint, {"2026-09-10": 99}), set())

    def test_completed_partial_is_not_skipped(self):
        checkpoint = self._checkpoint("completed_partial", listed=True)
        self.assertEqual(completed_dates_from_checkpoint_and_db(checkpoint, {"2026-09-10": 99}), set())

    def test_target_reached_with_rows_is_skipped(self):
        checkpoint = self._checkpoint("completed_target_reached", listed=True)
        self.assertEqual(
            completed_dates_from_checkpoint_and_db(checkpoint, {"2026-09-10": 150}),
            {"2026-09-10"},
        )

    def test_results_exhausted_is_skipped(self):
        checkpoint = self._checkpoint("completed_results_exhausted")
        self.assertEqual(
            completed_dates_from_checkpoint_and_db(checkpoint, {"2026-09-10": 25}),
            {"2026-09-10"},
        )

    def test_bare_completed_date_is_not_trusted(self):
        checkpoint = self._checkpoint(listed=True)
        self.assertEqual(completed_dates_from_checkpoint_and_db(checkpoint, {"2026-09-10": 99}), set())

    def test_old_rows_remain_available_for_deduplication(self):
        checkpoint = self._checkpoint("completed_partial", listed=True)
        counts = {"2026-09-10": 99}
        self.assertEqual(completed_dates_from_checkpoint_and_db(checkpoint, counts), set())
        self.assertEqual(counts["2026-09-10"], 99)

    def test_network_completion_status_is_preserved_exactly(self):
        checkpoint = self._checkpoint()
        update_checkpoint_date(
            checkpoint,
            stay_date=date(2026, 9, 10),
            checkout_date=date(2026, 9, 11),
            status="completed_target_reached",
            records_collected=150,
        )
        self.assertEqual(checkpoint["date_statuses"]["2026-09-10"]["status"], "completed_target_reached")
        update_checkpoint_date(
            checkpoint,
            stay_date=date(2026, 9, 11),
            checkout_date=date(2026, 9, 12),
            status="completed_results_exhausted",
            records_collected=25,
        )
        self.assertEqual(checkpoint["date_statuses"]["2026-09-11"]["status"], "completed_results_exhausted")

    def test_resumable_status_removes_stale_completed_date_entry(self):
        checkpoint = self._checkpoint("completed_target_reached", listed=True)
        update_checkpoint_date(
            checkpoint,
            stay_date=date(2026, 9, 10),
            checkout_date=date(2026, 9, 11),
            status="completed_resource_safe_limit",
            records_collected=99,
        )
        self.assertNotIn("2026-09-10", checkpoint["completed_dates"])


if __name__ == "__main__":
    unittest.main()
