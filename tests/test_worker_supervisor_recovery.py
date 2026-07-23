from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.job_runner import atomic_write_json
from services.worker_protocol import read_json
from tools.ota_worker_supervisor import RESOURCE_RECOVERY_EXIT, WorkerSupervisor


class ExitedChild:
    def __init__(self, returncode: int) -> None:
        self.pid = 321
        self.returncode = returncode

    def poll(self) -> int:
        return self.returncode


class WorkerSupervisorRecoveryTests(unittest.TestCase):
    def make_supervisor(self, root: Path) -> WorkerSupervisor:
        environment = {
            "INSTANCE_ID": "instance_test",
            "INSTANCE_DATA_DIR": str(root),
        }
        with patch.dict(os.environ, environment, clear=False):
            supervisor = WorkerSupervisor()
        active = {
            "job_id": "job-1",
            "request_id": "request-1",
            "instance_id": "instance_test",
            "state": "running",
        }
        atomic_write_json(supervisor.paths.active_job, active)
        return supervisor

    def set_current_status(self, supervisor: WorkerSupervisor, status: str) -> None:
        atomic_write_json(
            supervisor.data_dir / "status" / "current_job_status.json",
            {"job_id": "job-1", "status": status},
        )

    def test_watchdog_cancellation_is_recoverable_even_if_child_reports_user_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.make_supervisor(Path(directory))
            self.set_current_status(supervisor, "stopped_by_user_with_partial_results")
            atomic_write_json(
                supervisor.paths.cancel,
                {
                    "job_id": "job-1",
                    "cancel_requested": True,
                    "reason": "stale_heartbeat",
                },
            )
            supervisor.child = ExitedChild(0)

            supervisor.handle_child_exit()

            active = read_json(supervisor.paths.active_job)
            status = read_json(supervisor.paths.status)
            self.assertEqual(active["state"], "waiting_for_recovery")
            self.assertEqual(active["last_error"], "stale_heartbeat")
            self.assertEqual(status["status"], "waiting_for_recovery")

    def test_explicit_user_stop_remains_stopped_and_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.make_supervisor(Path(directory))
            self.set_current_status(supervisor, "stopped_by_user_with_partial_results")
            atomic_write_json(
                supervisor.paths.cancel,
                {
                    "job_id": "job-1",
                    "cancel_requested": True,
                    "reason": "user",
                },
            )
            supervisor.stop_requested = True
            supervisor.child = ExitedChild(0)

            supervisor.handle_child_exit()

            active = read_json(supervisor.paths.active_job)
            self.assertEqual(active["state"], "stopped_by_user")

    def test_resource_exit_waits_for_memory_recovery_and_preserves_job(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.make_supervisor(Path(directory))
            self.set_current_status(supervisor, "stopped_resource_limit")
            supervisor.child = ExitedChild(RESOURCE_RECOVERY_EXIT)

            supervisor.handle_child_exit()

            active = read_json(supervisor.paths.active_job)
            self.assertEqual(active["state"], "waiting_for_recovery")
            self.assertEqual(active["last_error"], "resource_limit")
            self.assertEqual(active["job_id"], "job-1")

    def test_no_progress_watchdog_uses_operation_specific_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.make_supervisor(Path(directory))
            atomic_write_json(
                supervisor.data_dir / "status" / "current_job_status.json",
                {
                    "job_id": "job-1",
                    "status": "running",
                    "current_date": "2026-08-01",
                    "current_step": "exporting latest CSV",
                    "progress_percent": 80,
                    "partial_records_saved": 100,
                },
            )
            supervisor.child = ExitedChild(0)
            supervisor.child.returncode = None
            self.assertFalse(supervisor.progress_stale())
            supervisor.last_progress_at -= supervisor.progress_stale_seconds + 1

            self.assertTrue(supervisor.progress_stale())
            self.assertEqual(supervisor.stalled_operation_reason(), "export_frozen")


if __name__ == "__main__":
    unittest.main()
