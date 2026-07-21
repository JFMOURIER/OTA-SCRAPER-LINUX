from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from services.job_runner import CancellationSignal


def _observe_signal(signal, output):
    for _ in range(50):
        if signal.is_set():
            output.put(True)
            return
        import time

        time.sleep(0.02)
    output.put(False)


class CancellationSignalTests(unittest.TestCase):
    def test_event_and_file_are_both_set(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cancel.json"
            signal = CancellationSignal(context.Event(), path, "job-1")
            signal.reset()
            self.assertFalse(signal.is_set())
            signal.set("user")
            self.assertTrue(signal.is_set())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(payload["cancel_requested"])
            self.assertEqual(payload["reason"], "user")

    def test_other_job_file_does_not_cancel(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cancel.json"
            path.write_text('{"job_id":"other","cancel_requested":true}', encoding="utf-8")
            signal = CancellationSignal(context.Event(), path, "job-1")
            self.assertFalse(signal.is_set())

    def test_signal_reaches_spawned_worker(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            signal = CancellationSignal(context.Event(), Path(directory) / "cancel.json", "job-1")
            signal.reset()
            output = context.Queue()
            process = context.Process(target=_observe_signal, args=(signal, output))
            process.start()
            signal.set("user")
            process.join(timeout=3)
            self.assertFalse(process.is_alive())
            self.assertTrue(output.get(timeout=1))


if __name__ == "__main__":
    unittest.main()
