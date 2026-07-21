from __future__ import annotations

import fcntl
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import psutil


MB = 1024 * 1024
GB = 1024 * MB
BROWSER_MARKERS = ("chromium", "chrome", "playwright", "headless_shell")


class ResourceLimitExceeded(RuntimeError):
    """Raised when continuing would risk another machine-wide OOM or full disk."""

    status = "stopped_resource_limit"

    def __init__(self, message: str, snapshot: "ResourceSnapshot") -> None:
        super().__init__(message)
        self.snapshot = snapshot


class ScraperAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResourceThresholds:
    minimum_start_available_mb: int = 800
    emergency_available_mb: int = 500
    minimum_swap_free_mb: int = 256
    maximum_swap_percent: float = 94.0
    warn_python_rss_mb: int = 1600
    stop_python_rss_mb: int = 2400
    warn_browser_rss_mb: int = 1800
    stop_browser_rss_mb: int = 2600
    minimum_disk_free_gb: float = 5.0

    @classmethod
    def from_environment(cls) -> "ResourceThresholds":
        def integer(name: str, default: int) -> int:
            try:
                return max(0, int(os.getenv(name, str(default))))
            except ValueError:
                return default

        def decimal(name: str, default: float) -> float:
            try:
                return max(0.0, float(os.getenv(name, str(default))))
            except ValueError:
                return default

        return cls(
            minimum_start_available_mb=integer("OTA_MIN_START_AVAILABLE_MB", 800),
            emergency_available_mb=integer("OTA_EMERGENCY_AVAILABLE_MB", 500),
            minimum_swap_free_mb=integer("OTA_MIN_SWAP_FREE_MB", 256),
            maximum_swap_percent=decimal("OTA_MAX_SWAP_PERCENT", 94.0),
            warn_python_rss_mb=integer("OTA_WARN_PYTHON_RSS_MB", 1600),
            stop_python_rss_mb=integer("OTA_STOP_PYTHON_RSS_MB", 2400),
            warn_browser_rss_mb=integer("OTA_WARN_BROWSER_RSS_MB", 1800),
            stop_browser_rss_mb=integer("OTA_STOP_BROWSER_RSS_MB", 2600),
            minimum_disk_free_gb=decimal("OTA_MIN_DISK_FREE_GB", 5.0),
        )


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    timestamp: str
    owner_pid: int
    available_ram_mb: float
    swap_used_mb: float
    swap_free_mb: float
    swap_percent: float
    python_rss_mb: float
    browser_rss_mb: float
    browser_process_count: int
    disk_free_gb: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _browser_descendants(owner_pid: int) -> list[psutil.Process]:
    try:
        descendants = psutil.Process(owner_pid).children(recursive=True)
    except (psutil.Error, OSError):
        return []
    matched: list[psutil.Process] = []
    for process in descendants:
        try:
            command = " ".join(process.cmdline()).lower()
            name = process.name().lower()
        except (psutil.Error, OSError):
            continue
        if any(marker in name or marker in command for marker in BROWSER_MARKERS):
            matched.append(process)
    return matched


def resource_snapshot(data_path: str | Path, owner_pid: int | None = None) -> ResourceSnapshot:
    owner_pid = int(owner_pid or os.getpid())
    virtual_memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    try:
        python_rss = psutil.Process(owner_pid).memory_info().rss
    except (psutil.Error, OSError):
        python_rss = 0
    browser_processes = _browser_descendants(owner_pid)
    browser_rss = 0
    for process in browser_processes:
        try:
            browser_rss += process.memory_info().rss
        except (psutil.Error, OSError):
            continue
    disk = shutil.disk_usage(Path(data_path))
    return ResourceSnapshot(
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        owner_pid=owner_pid,
        available_ram_mb=round(virtual_memory.available / MB, 1),
        swap_used_mb=round(swap.used / MB, 1),
        swap_free_mb=round(swap.free / MB, 1),
        swap_percent=round(float(swap.percent), 1),
        python_rss_mb=round(python_rss / MB, 1),
        browser_rss_mb=round(browser_rss / MB, 1),
        browser_process_count=len(browser_processes),
        disk_free_gb=round(disk.free / GB, 2),
    )


def evaluate_snapshot(
    snapshot: ResourceSnapshot,
    thresholds: ResourceThresholds,
    *,
    starting: bool = False,
) -> tuple[str, str]:
    if snapshot.available_ram_mb < thresholds.emergency_available_mb:
        return "emergency", f"available RAM is {snapshot.available_ram_mb:.0f} MB"
    if snapshot.swap_free_mb < thresholds.minimum_swap_free_mb or snapshot.swap_percent >= thresholds.maximum_swap_percent:
        return "emergency", f"swap is {snapshot.swap_percent:.1f}% used with {snapshot.swap_free_mb:.0f} MB free"
    if snapshot.disk_free_gb < thresholds.minimum_disk_free_gb:
        return "emergency", f"disk has only {snapshot.disk_free_gb:.2f} GB free"
    if starting and snapshot.available_ram_mb < thresholds.minimum_start_available_mb:
        return "stop", f"available RAM is below the {thresholds.minimum_start_available_mb} MB start threshold"
    if snapshot.python_rss_mb >= thresholds.stop_python_rss_mb:
        return "stop", f"worker RSS is {snapshot.python_rss_mb:.0f} MB"
    if snapshot.browser_rss_mb >= thresholds.stop_browser_rss_mb:
        return "stop", f"scraper browser RSS is {snapshot.browser_rss_mb:.0f} MB"
    warnings: list[str] = []
    if snapshot.python_rss_mb >= thresholds.warn_python_rss_mb:
        warnings.append(f"worker RSS {snapshot.python_rss_mb:.0f} MB")
    if snapshot.browser_rss_mb >= thresholds.warn_browser_rss_mb:
        warnings.append(f"browser RSS {snapshot.browser_rss_mb:.0f} MB")
    return ("warning", "; ".join(warnings)) if warnings else ("ok", "within configured limits")


class ResourceGuard:
    def __init__(
        self,
        data_path: str | Path,
        *,
        owner_pid: int | None = None,
        thresholds: ResourceThresholds | None = None,
        minimum_check_interval_seconds: float = 3.0,
    ) -> None:
        self.data_path = Path(data_path)
        self.owner_pid = int(owner_pid or os.getpid())
        self.thresholds = thresholds or ResourceThresholds.from_environment()
        self.minimum_check_interval_seconds = minimum_check_interval_seconds
        self.last_snapshot: ResourceSnapshot | None = None
        self.last_level = "ok"
        self.last_reason = "not checked"
        self._last_check = 0.0
        self.peak_python_rss_mb = 0.0
        self.peak_browser_rss_mb = 0.0
        self.peak_browser_process_count = 0
        self.minimum_available_ram_mb = float("inf")

    def check(self, *, starting: bool = False, force: bool = False) -> ResourceSnapshot:
        now = time.monotonic()
        if not force and not starting and self.last_snapshot and now - self._last_check < self.minimum_check_interval_seconds:
            return self.last_snapshot
        snapshot = resource_snapshot(self.data_path, self.owner_pid)
        level, reason = evaluate_snapshot(snapshot, self.thresholds, starting=starting)
        self.last_snapshot = snapshot
        self.peak_python_rss_mb = max(self.peak_python_rss_mb, snapshot.python_rss_mb)
        self.peak_browser_rss_mb = max(self.peak_browser_rss_mb, snapshot.browser_rss_mb)
        self.peak_browser_process_count = max(self.peak_browser_process_count, snapshot.browser_process_count)
        self.minimum_available_ram_mb = min(self.minimum_available_ram_mb, snapshot.available_ram_mb)
        self.last_level = level
        self.last_reason = reason
        self._last_check = now
        if level in {"stop", "emergency"}:
            raise ResourceLimitExceeded(f"Resource guard {level}: {reason}", snapshot)
        return snapshot

    def peaks(self) -> dict[str, Any]:
        return {
            "peak_python_rss_mb": round(self.peak_python_rss_mb, 1),
            "peak_browser_rss_mb": round(self.peak_browser_rss_mb, 1),
            "peak_browser_process_count": self.peak_browser_process_count,
            "minimum_available_ram_mb": None if self.minimum_available_ram_mb == float("inf") else round(self.minimum_available_ram_mb, 1),
        }


class SingleScraperLock:
    """An advisory host-wide lock. The open descriptor owns the lock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self, job_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "unknown owner"
            handle.close()
            raise ScraperAlreadyRunning(f"Another scraper owns {self.path}: {owner}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} job_id={job_id} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "SingleScraperLock":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def cleanup_owned_browser_processes(
    owner_pid: int,
    log: Callable[[str], None] | None = None,
    *,
    terminate_timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    processes = _browser_descendants(owner_pid)
    targeted = [process.pid for process in processes]
    if log:
        log(f"Browser cleanup: {len(processes)} scraper-owned descendant(s) found: {targeted}")
    for process in processes:
        try:
            process.terminate()
            if log:
                log(f"Browser cleanup: SIGTERM sent to scraper-owned PID {process.pid}")
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            if log:
                log(f"Browser cleanup: could not terminate PID {process.pid}: {exc}")
    gone, alive = psutil.wait_procs(processes, timeout=terminate_timeout_seconds)
    killed: list[int] = []
    for process in alive:
        try:
            process.kill()
            killed.append(process.pid)
            if log:
                log(f"Browser cleanup: SIGKILL sent after timeout to scraper-owned PID {process.pid}")
        except psutil.Error:
            continue
    if alive:
        psutil.wait_procs(alive, timeout=1.0)
    return {"targeted": targeted, "terminated": [process.pid for process in gone], "killed": killed}
