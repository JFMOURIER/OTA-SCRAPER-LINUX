from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import psutil

from services.scraper_errors import is_browser_closed_error


LogFn = Callable[[str], None] | None
BROWSER_MARKERS = ("chrome", "chromium", "playwright", "headless_shell")


def safe_page_url(page: Any, log: LogFn = None) -> str | None:
    try:
        return page.url
    except Exception as exc:
        if log:
            log(f"Warning: could not read page URL: {exc}")
        return None


def safe_page_title(page: Any, log: LogFn = None) -> str | None:
    try:
        return page.title()
    except Exception as exc:
        if log:
            log(f"Warning: could not read page title: {exc}")
        return None


def safe_screenshot(page: Any, path: str | Path, label: str = "screenshot", log: LogFn = None, *, full_page: bool = False) -> bool:
    try:
        if page is None or (hasattr(page, "is_closed") and page.is_closed()):
            if log:
                log(f"Warning: skipped {label}; page is already closed.")
            return False
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=full_page, animations="disabled", timeout=10000)
        return True
    except Exception as exc:
        prefix = "browser closed" if is_browser_closed_error(exc) else "failed"
        if log:
            log(f"Warning: {label} screenshot {prefix}: {exc}")
        return False


def safe_close_browser(browser: Any = None, context: Any = None, page: Any = None, log: LogFn = None) -> None:
    for label, target in (("page", page), ("context", context), ("browser", browser)):
        if target is None:
            continue
        try:
            if label == "page" and hasattr(target, "is_closed") and target.is_closed():
                continue
            target.close()
            if log:
                log(f"Browser cleanup: closed Playwright {label}.")
        except Exception as exc:
            if log:
                log(f"Warning: ignored {label} close failure: {exc}")


def find_owned_browser_processes(
    owner_pid: int,
    *,
    profile_dir: str | Path | None = None,
    job_token: str | None = None,
) -> list[psutil.Process]:
    """Find only Chromium owned by this scraper tree or its exact launch markers.

    Chromium helpers can be reparented when the Playwright driver exits.  An
    exact ``--user-data-dir``/job-token match recovers those processes without
    ever selecting an unrelated interactive Chrome session.
    """

    found: dict[int, psutil.Process] = {}
    try:
        descendants = psutil.Process(int(owner_pid)).children(recursive=True)
    except (psutil.Error, OSError):
        descendants = []
    exact_profile = str(Path(profile_dir).resolve()) if profile_dir else ""
    token = str(job_token or "").strip()
    candidates: list[psutil.Process] = list(descendants)
    if exact_profile or token:
        candidates.extend(psutil.process_iter(["pid", "name", "cmdline"]))
    for process in candidates:
        try:
            name = str(process.info.get("name") if hasattr(process, "info") else process.name()).lower()
            cmdline_values = process.info.get("cmdline") if hasattr(process, "info") else process.cmdline()
            command = " ".join(cmdline_values or [])
            command_lower = command.lower()
        except (psutil.Error, OSError, AttributeError):
            continue
        if not any(marker in name or marker in command_lower for marker in BROWSER_MARKERS):
            continue
        descendant_owned = process in descendants
        profile_owned = bool(exact_profile and exact_profile in command)
        token_owned = bool(token and token in command)
        if descendant_owned or profile_owned or token_owned:
            found[process.pid] = process
    return [found[pid] for pid in sorted(found)]


def wait_for_owned_browser_exit(
    owner_pid: int,
    *,
    profile_dir: str | Path | None = None,
    job_token: str | None = None,
    timeout_seconds: float = 3.0,
    log: LogFn = None,
) -> dict[str, Any]:
    """Wait, then terminate only marked scraper Chromium that survived close."""

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    processes = find_owned_browser_processes(owner_pid, profile_dir=profile_dir, job_token=job_token)
    while processes and time.monotonic() < deadline:
        time.sleep(0.1)
        processes = find_owned_browser_processes(owner_pid, profile_dir=profile_dir, job_token=job_token)
    remaining_before = [process.pid for process in processes]
    for process in processes:
        try:
            process.terminate()
            if log:
                log(f"Browser cleanup: SIGTERM sent to owned Chromium PID {process.pid}.")
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            if log:
                log(f"Browser cleanup: could not terminate owned PID {process.pid}: {exc}")
    _gone, alive = psutil.wait_procs(processes, timeout=2.0)
    killed: list[int] = []
    for process in alive:
        try:
            process.kill()
            killed.append(process.pid)
            if log:
                log(f"Browser cleanup: SIGKILL sent to owned Chromium PID {process.pid}.")
        except psutil.Error:
            continue
    if alive:
        psutil.wait_procs(alive, timeout=1.0)
    after = find_owned_browser_processes(owner_pid, profile_dir=profile_dir, job_token=job_token)
    payload = {
        "owned_before_cleanup": remaining_before,
        "terminated": [process.pid for process in processes if process.pid not in killed],
        "killed": killed,
        "owned_after_cleanup": [process.pid for process in after],
    }
    if log:
        log(
            "Browser cleanup verification: "
            f"before={len(remaining_before)} after={len(payload['owned_after_cleanup'])} "
            f"remaining={payload['owned_after_cleanup']}"
        )
    return payload


def _profile_lock_owner(lock_path: Path) -> tuple[int | None, str]:
    try:
        target = os.readlink(lock_path) if lock_path.is_symlink() else lock_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        target = ""
    matches = re.findall(r"(?:^|[-_])(\d{2,})(?:$|[-_])", target)
    pid = int(matches[-1]) if matches else None
    if pid is None:
        return None, target
    try:
        process = psutil.Process(pid)
        command = " ".join(process.cmdline()).lower()
        if any(marker in command for marker in ("chrome", "chromium", "playwright", "headless_shell")):
            return pid, target
    except psutil.Error:
        pass
    return None, target


def prepare_profile_directory(profile_dir: str | Path, log: LogFn = None) -> Path:
    """Create a unique profile and preserve stale locks under timestamped names."""

    path = Path(profile_dir)
    path.mkdir(parents=True, exist_ok=True)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = path / name
        if not lock_path.exists() and not lock_path.is_symlink():
            continue
        owner_pid, target = _profile_lock_owner(lock_path)
        if owner_pid is not None:
            raise RuntimeError(
                f"Browser profile is in use by Chromium PID {owner_pid}: {path}. "
                "The lock was left untouched."
            )
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        preserved = lock_path.with_name(f"{lock_path.name}.stale_{suffix}")
        lock_path.rename(preserved)
        if log:
            log(f"Preserved stale browser profile lock {lock_path.name} as {preserved.name}; previous target={target!r}")
    return path


def _channel_unavailable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "executable doesn't exist",
            "executable does not exist",
            "chromium distribution 'chromium' is not found",
            "browser distribution 'chromium' is not found",
            "unknown channel",
        )
    )


def launch_managed_chromium_context(
    playwright: Any,
    *,
    headless: bool,
    profile_dir: str | Path | None,
    args: list[str] | None = None,
    viewport: dict[str, int] | None = None,
    log: LogFn = None,
) -> tuple[Any | None, Any, str]:
    """Use full Chromium/new headless first and report an explicit tested fallback."""

    launch_args = list(args or [])
    viewport = viewport or {"width": 1366, "height": 900}
    profile = prepare_profile_directory(profile_dir, log) if profile_dir else None

    def launch(channel: str | None) -> tuple[Any | None, Any]:
        channel_kwargs = {"channel": channel} if channel else {}
        if profile is not None:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=headless,
                args=launch_args,
                viewport=viewport,
                **channel_kwargs,
            )
            return None, context
        browser = playwright.chromium.launch(headless=headless, args=launch_args, **channel_kwargs)
        return browser, browser.new_context(viewport=viewport)

    try:
        browser, context = launch("chromium")
        implementation = "chromium-channel-new-headless" if headless else "chromium-channel-visible"
        if log:
            log(f"Playwright browser implementation: {implementation}")
        return browser, context, implementation
    except Exception as exc:
        if not _channel_unavailable(exc):
            raise
        if log:
            log(f"Chromium channel unavailable ({exc}); testing bundled Playwright fallback now.")
        browser, context = launch(None)
        implementation = "playwright-bundled-fallback"
        if log:
            log(f"Playwright browser implementation: {implementation} (fallback launch succeeded)")
        return browser, context, implementation


def interruptible_page_wait(page: Any, milliseconds: int, stop_event: Any = None, *, quantum_ms: int = 250) -> bool:
    """Return False as soon as cancellation is signalled."""

    remaining = max(0, int(milliseconds))
    while remaining > 0:
        if stop_event is not None and stop_event.is_set():
            return False
        delay = min(remaining, quantum_ms)
        page.wait_for_timeout(delay)
        remaining -= delay
    return stop_event is None or not stop_event.is_set()
