#!/usr/bin/env python3
"""Persistent per-instance scraper supervisor.

The supervisor owns scraper children, not Streamlit. Requests and acknowledgments
are atomic JSON files. Only the current child's process group is ever signalled.
"""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psutil

from services.job_runner import atomic_write_json, now_text
from services.resource_guard import ResourceThresholds
from services.worker_protocol import WorkerPaths, job_is_terminal, read_json, validate_request, write_status


RESOURCE_RECOVERY_EXIT = 75
UNEXPECTED_EXIT = 70


class WorkerSupervisor:
    def __init__(self) -> None:
        self.instance_id = os.getenv("INSTANCE_ID", "instance_1")
        self.data_dir = Path(
            os.getenv("INSTANCE_DATA_DIR", str(ROOT / "data" / "instances" / self.instance_id))
        ).resolve()
        self.paths = WorkerPaths(self.data_dir / "status")
        self.paths.status_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.data_dir / "logs" / "worker_supervisor.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.child: subprocess.Popen[str] | None = None
        self.child_log = None
        self.stop_requested = False
        self.stop_deadline: float | None = None
        self.shutdown = False
        self.last_request_id = str(read_json(self.paths.status).get("acknowledged_request_id") or "")
        self.restart_times = [
            float(value)
            for value in read_json(self.paths.restart_history).get("timestamps", [])
            if isinstance(value, (int, float))
        ]
        self.thresholds = ResourceThresholds.from_environment()
        self.stale_seconds = max(60, int(os.getenv("OTA_WORKER_HEARTBEAT_STALE_SECONDS", "180")))
        self.progress_stale_seconds = max(
            self.stale_seconds,
            int(os.getenv("OTA_WORKER_PROGRESS_STALE_SECONDS", "600")),
        )
        self.grace_seconds = max(10, int(os.getenv("OTA_WORKER_STOP_GRACE_SECONDS", "45")))
        self.lock_handle = None
        self.last_progress_fingerprint: tuple[Any, ...] | None = None
        self.last_progress_at = time.monotonic()

    def log(self, message: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_text()}] {message}\n")
            handle.flush()

    def acquire(self) -> None:
        self.lock_handle = self.paths.lock.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another supervisor owns {self.paths.lock}") from exc
        self.lock_handle.seek(0)
        self.lock_handle.truncate()
        self.lock_handle.write(f"pid={os.getpid()} instance={self.instance_id} started={now_text()}\n")
        self.lock_handle.flush()
        os.fsync(self.lock_handle.fileno())
        atomic_write_json(self.paths.supervisor_pid, {"pid": os.getpid(), "started_at": now_text()})
        write_status(self.paths, supervisor_pid=os.getpid(), supervisor_status="idle")

    def release(self) -> None:
        if self.lock_handle:
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_UN)
            self.lock_handle.close()
            self.lock_handle = None

    def process_request(self) -> None:
        raw = read_json(self.paths.request)
        request_id = str(raw.get("request_id") or "")
        if not request_id or request_id == self.last_request_id:
            return
        try:
            request = validate_request(raw, self.instance_id)
            action = request["action"]
            if action == "stop":
                self.request_child_stop(request_id, "user")
            elif action in {"start", "resume"}:
                if self.child and self.child.poll() is None:
                    raise RuntimeError("this instance already has an active scraper child")
                request["action"] = action
                request["supervisor_resume"] = action == "resume"
                request["state"] = "requested"
                request["restart_count"] = 0
                request["last_error"] = None
                atomic_write_json(self.paths.active_job, request)
                self.stop_requested = False
                write_status(
                    self.paths,
                    status="acknowledged",
                    active_job_id=request.get("job_id"),
                    acknowledged_request_id=request_id,
                    last_error=None,
                )
            elif action == "clear_completed":
                active = read_json(self.paths.active_job)
                if not raw.get("confirmed"):
                    raise RuntimeError("clear_completed requires confirmed=true")
                if active and not job_is_terminal(active.get("state")):
                    raise RuntimeError("active job is not terminal")
                atomic_write_json(self.paths.active_job, {"state": "cleared", "cleared_at": now_text()})
                write_status(self.paths, status="idle", acknowledged_request_id=request_id)
            self.last_request_id = request_id
        except Exception as exc:
            self.last_request_id = request_id
            write_status(
                self.paths,
                status="request_rejected",
                acknowledged_request_id=request_id,
                last_error=str(exc),
            )
            self.log(f"Request {request_id or '<missing>'} rejected: {exc}")

    def request_child_stop(self, request_id: str, reason: str) -> None:
        active = read_json(self.paths.active_job)
        job_id = str(active.get("job_id") or read_json(self.paths.status).get("active_job_id") or "")
        atomic_write_json(
            self.paths.cancel,
            {
                "job_id": job_id,
                "cancel_requested": True,
                "reason": reason,
                "updated_at": now_text(),
            },
        )
        self.stop_requested = reason == "user"
        self.stop_deadline = time.monotonic() + self.grace_seconds
        write_status(
            self.paths,
            status="stopping",
            acknowledged_request_id=request_id,
            stop_reason=reason,
        )
        self.last_request_id = request_id
        self.log(f"Graceful {reason} stop requested for child PID {self.child.pid if self.child else None}.")

    def resource_recovered(self) -> bool:
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        available_mb = memory.available / (1024 * 1024)
        swap_pressure = (
            swap.free / (1024 * 1024) < self.thresholds.minimum_swap_free_mb
            or swap.percent >= self.thresholds.maximum_swap_percent
        )
        required = self.thresholds.minimum_start_available_mb
        if swap_pressure:
            required = max(
                required,
                self.thresholds.swap_pressure_available_mb
                + self.thresholds.soft_recovery_hysteresis_mb,
            )
        return available_mb >= required

    def restart_budget_available(self) -> bool:
        now = time.time()
        self.restart_times = [value for value in self.restart_times if now - value < 3600]
        atomic_write_json(self.paths.restart_history, {"timestamps": self.restart_times})
        return len(self.restart_times) < 5

    def launch_child(self, active: dict[str, Any], *, recovery_reason: str | None = None) -> None:
        if recovery_reason:
            if not self.restart_budget_available():
                active["state"] = "failed_restart_limit"
                active["last_error"] = recovery_reason
                atomic_write_json(self.paths.active_job, active)
                write_status(self.paths, status="failed_restart_limit", last_error=recovery_reason)
                return
            self.restart_times.append(time.time())
            active["restart_count"] = int(active.get("restart_count") or 0) + 1
            active["last_restart_reason"] = recovery_reason
            active["supervisor_resume"] = True
            active["action"] = "resume"
            atomic_write_json(self.paths.restart_history, {"timestamps": self.restart_times})
        active["state"] = "running"
        active["last_started_at"] = now_text()
        atomic_write_json(self.paths.active_job, active)
        self.last_progress_fingerprint = None
        self.last_progress_at = time.monotonic()
        self.child_log = self.log_path.open("a", encoding="utf-8")
        command = [
            str(ROOT / ".venv" / "bin" / "python"),
            str(ROOT / "tools" / "ota_worker_child.py"),
            str(self.paths.active_job),
        ]
        self.child = subprocess.Popen(
            command,
            cwd=ROOT,
            start_new_session=True,
            stdout=self.child_log,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
            text=True,
        )
        atomic_write_json(
            self.paths.child_pid,
            {"pid": self.child.pid, "pgid": os.getpgid(self.child.pid), "started_at": now_text()},
        )
        write_status(
            self.paths,
            status="running",
            scraper_child_pid=self.child.pid,
            scraper_child_pgid=os.getpgid(self.child.pid),
            restart_count=int(active.get("restart_count") or 0),
            last_restart_reason=recovery_reason,
        )
        self.log(
            f"Started scraper child PID {self.child.pid} PGID {os.getpgid(self.child.pid)} "
            f"restart_reason={recovery_reason!r}."
        )

    def terminate_child_group(self, reason: str) -> None:
        if not self.child or self.child.poll() is not None:
            return
        try:
            pgid = os.getpgid(self.child.pid)
            os.killpg(pgid, signal.SIGTERM)
            self.log(f"SIGTERM sent to scraper PGID {pgid}: {reason}.")
        except (ProcessLookupError, PermissionError):
            return
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
                self.log(f"SIGKILL sent to scraper PGID {pgid} after grace timeout.")
            except (ProcessLookupError, PermissionError):
                pass

    def heartbeat_stale(self) -> bool:
        heartbeat = read_json(self.paths.heartbeat)
        if not heartbeat or not self.child or self.child.poll() is not None:
            return False
        try:
            updated = time.mktime(time.strptime(str(heartbeat["last_updated_at"]), "%Y-%m-%d %H:%M:%S"))
        except (KeyError, TypeError, ValueError):
            return True
        return time.time() - updated > self.stale_seconds

    def progress_stale(self) -> bool:
        """Detect a live child that sends heartbeats but makes no durable progress."""

        if not self.child or self.child.poll() is not None:
            return False
        status = read_json(self.data_dir / "status" / "current_job_status.json")
        active = read_json(self.paths.active_job)
        if str(status.get("job_id") or "") != str(active.get("job_id") or ""):
            return False
        fingerprint = (
            status.get("current_date"),
            status.get("current_step"),
            status.get("progress_percent"),
            status.get("load_more_clicks"),
            status.get("current_hotel_card_count"),
            status.get("partial_records_saved"),
            status.get("hotels_collected_total"),
        )
        if fingerprint != self.last_progress_fingerprint:
            self.last_progress_fingerprint = fingerprint
            self.last_progress_at = time.monotonic()
            return False
        return time.monotonic() - self.last_progress_at > self.progress_stale_seconds

    def stalled_operation_reason(self) -> str:
        status = read_json(self.data_dir / "status" / "current_job_status.json")
        message = " ".join(
            str(status.get(field) or "")
            for field in ("current_step", "current_message")
        ).lower()
        if "export" in message or "csv" in message or "excel" in message:
            return "export_frozen"
        if any(
            marker in message
            for marker in ("browser", "chromium", "navigation", "scroll", "load more", "extract")
        ):
            return "browser_frozen"
        return "no_progress"

    def handle_child_exit(self) -> None:
        if not self.child or self.child.poll() is None:
            return
        code = int(self.child.returncode or 0)
        if self.child_log:
            self.child_log.close()
            self.child_log = None
        active = read_json(self.paths.active_job)
        job_status = str(read_json(self.data_dir / "status" / "current_job_status.json").get("status") or "")
        cancel = read_json(self.paths.cancel)
        cancel_reason = (
            str(cancel.get("reason") or "")
            if cancel.get("cancel_requested")
            and str(cancel.get("job_id") or "") == str(active.get("job_id") or "")
            else ""
        )
        supervisor_recovery = cancel_reason in {
            "stale_heartbeat",
            "no_progress",
            "browser_frozen",
            "export_frozen",
        }
        self.log(
            f"Scraper child exited code={code} job_status={job_status!r} "
            f"cancel_reason={cancel_reason!r}."
        )
        self.child = None
        self.stop_deadline = None
        if self.stop_requested or (
            job_status.startswith("stopped_by_user") and not supervisor_recovery
        ):
            active["state"] = "stopped_by_user"
            atomic_write_json(self.paths.active_job, active)
            write_status(self.paths, status="stopped_by_user", scraper_child_pid=None, scraper_child_pgid=None)
            self.stop_requested = False
            return
        if supervisor_recovery:
            active["state"] = "waiting_for_recovery"
            active["last_error"] = cancel_reason
            atomic_write_json(self.paths.active_job, active)
            write_status(
                self.paths,
                status="waiting_for_recovery",
                last_error=cancel_reason,
                scraper_child_pid=None,
                scraper_child_pgid=None,
            )
            return
        if job_is_terminal(job_status):
            active["state"] = job_status
            atomic_write_json(self.paths.active_job, active)
            write_status(self.paths, status=job_status, scraper_child_pid=None, scraper_child_pgid=None)
            return
        reason = (
            "resource_limit"
            if code == RESOURCE_RECOVERY_EXIT or job_status == "stopped_resource_limit"
            else f"unexpected_child_exit_{code}"
        )
        active["state"] = "waiting_for_recovery"
        active["last_error"] = reason
        atomic_write_json(self.paths.active_job, active)
        write_status(
            self.paths,
            status="waiting_for_recovery",
            last_error=reason,
            scraper_child_pid=None,
            scraper_child_pgid=None,
        )

    def run(self) -> int:
        self.acquire()
        try:
            while not self.shutdown:
                self.process_request()
                self.handle_child_exit()
                active = read_json(self.paths.active_job)
                state = str(active.get("state") or "")
                if self.child and self.child.poll() is None:
                    if self.heartbeat_stale() and not self.stop_deadline:
                        self.request_child_stop(self.last_request_id or "watchdog", "stale_heartbeat")
                    elif self.progress_stale() and not self.stop_deadline:
                        self.request_child_stop(
                            self.last_request_id or "watchdog",
                            self.stalled_operation_reason(),
                        )
                    if self.stop_deadline and time.monotonic() >= self.stop_deadline:
                        recovery = not self.stop_requested
                        self.terminate_child_group("graceful cancellation timeout")
                        if recovery:
                            active["state"] = "waiting_for_recovery"
                            active["last_error"] = "stale_heartbeat"
                            atomic_write_json(self.paths.active_job, active)
                elif state in {"requested", "waiting_for_recovery"}:
                    if self.resource_recovered():
                        recovery_reason = active.get("last_error") if state == "waiting_for_recovery" else None
                        self.launch_child(active, recovery_reason=str(recovery_reason) if recovery_reason else None)
                    else:
                        write_status(self.paths, status="waiting_for_memory", supervisor_pid=os.getpid())
                write_status(
                    self.paths,
                    supervisor_pid=os.getpid(),
                    supervisor_status="running",
                    supervisor_heartbeat=now_text(),
                )
                time.sleep(2)
        finally:
            if self.child and self.child.poll() is None:
                self.terminate_child_group("supervisor shutdown")
            self.release()
        return 0


def main() -> int:
    supervisor = WorkerSupervisor()

    def stop(_signum: int, _frame: Any) -> None:
        supervisor.shutdown = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
