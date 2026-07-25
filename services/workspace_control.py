from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import psutil

from services.playwright_safe import find_owned_browser_processes


Runner = Callable[..., subprocess.CompletedProcess[str]]
SESSION_ENV_KEYS = (
    "DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "DESKTOP_SESSION",
    "XDG_CURRENT_DESKTOP",
    "DBUS_SESSION_BUS_ADDRESS",
)


def discover_cinnamon_session_environment() -> dict[str, str]:
    """Read display variables from the active same-user Cinnamon process."""

    candidates: list[psutil.Process] = []
    for process in psutil.process_iter(["pid", "name", "cmdline", "username"]):
        try:
            name = str(process.info.get("name") or "").lower()
            command = " ".join(process.info.get("cmdline") or []).lower()
            if name == "cinnamon" or command.startswith("cinnamon --replace"):
                candidates.append(process)
        except (psutil.Error, OSError):
            continue
    for process in sorted(candidates, key=lambda item: item.pid):
        try:
            raw = process.environ()
        except (psutil.Error, OSError):
            continue
        env = {
            key: str(raw[key])
            for key in SESSION_ENV_KEYS
            if raw.get(key)
        }
        desktop = (
            env.get("XDG_CURRENT_DESKTOP", "")
            + " "
            + env.get("DESKTOP_SESSION", "")
        ).lower()
        if "cinnamon" in desktop and env.get("DISPLAY"):
            return env
    return {}


def resolve_workspace_index(
    workspace_listing: str,
    requested_name: str,
) -> tuple[int | None, str | None]:
    rows: list[tuple[int, str]] = []
    for line in str(workspace_listing or "").splitlines():
        fields = line.split(None, 9)
        if not fields:
            continue
        try:
            index = int(fields[0])
        except (ValueError, TypeError):
            continue
        # wmctrl -d has nine fixed fields before the workspace name.
        name = fields[9].strip() if len(fields) > 9 else ""
        rows.append((index, name))
    for index, name in rows:
        if name == requested_name:
            return index, name
    requested_lower = requested_name.casefold()
    for index, name in rows:
        if name.casefold() == requested_lower:
            return index, name
    return None, None


def select_owned_window_ids(
    window_listing: str,
    *,
    owned_pids: Iterable[int],
    window_class: str,
) -> list[str]:
    """Select only the unique scraper class or its owned PID tree."""

    owned = {int(value) for value in owned_pids}
    marker = window_class.casefold()
    selected: list[str] = []
    for line in str(window_listing or "").splitlines():
        fields = line.split(None, 6)
        if len(fields) < 5:
            continue
        window_id = fields[0]
        try:
            pid = int(fields[2])
        except ValueError:
            continue
        class_text = fields[4].casefold()
        title = " ".join(fields[5:]).casefold()
        class_match = marker in {
            token
            for part in class_text.split(".")
            for token in (part, class_text)
        }
        # The exact custom class is sufficient by itself; otherwise require the
        # scraper-owned PID and a class/title marker.  Generic Chrome is never
        # selected merely because it is visible.
        if class_match or pid in owned:
            selected.append(window_id)
    return selected


def move_browser_windows_to_workspace(
    *,
    owner_pid: int,
    profile_dir: str | Path | None,
    workspace_name: str | None,
    window_class: str | None,
    headless: bool,
    poll_seconds: float = 5.0,
    runner: Runner = subprocess.run,
    session_env: dict[str, str] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    requested = str(workspace_name or "").strip() or None
    marker = str(window_class or "").strip() or None
    result: dict[str, Any] = {
        "workspace_requested": requested,
        "workspace_detected": None,
        "browser_window_id": None,
        "browser_window_workspace": None,
        "workspace_move_status": "not_requested",
        "browser_window_ids": [],
    }
    if headless:
        result["workspace_move_status"] = "headless_not_applicable"
        return result
    if not requested or not marker:
        return result
    env = dict(session_env or discover_cinnamon_session_environment())
    session_type = env.get("XDG_SESSION_TYPE", "").lower()
    desktop = (
        env.get("XDG_CURRENT_DESKTOP", "")
        + " "
        + env.get("DESKTOP_SESSION", "")
    ).lower()
    if session_type == "wayland":
        result["workspace_move_status"] = "unsupported_wayland"
        return result
    if session_type != "x11" or "cinnamon" not in desktop:
        result["workspace_move_status"] = "cinnamon_x11_not_detected"
        return result
    command_env = os.environ.copy()
    command_env.update(env)
    try:
        workspaces = runner(
            ["wmctrl", "-d"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=command_env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result["workspace_move_status"] = f"wmctrl_unavailable: {exc}"
        return result
    if workspaces.returncode != 0:
        result["workspace_move_status"] = (
            "workspace_list_failed: "
            + (workspaces.stderr.strip() or "wmctrl -d failed")
        )
        return result
    workspace_index, detected = resolve_workspace_index(
        workspaces.stdout,
        requested,
    )
    result["workspace_detected"] = detected
    if workspace_index is None:
        result["workspace_move_status"] = "workspace_not_found"
        if log:
            log(f"Workspace warning: {requested!r} does not exist.")
        return result

    deadline = time.monotonic() + max(0.0, poll_seconds)
    window_ids: list[str] = []
    while time.monotonic() <= deadline:
        owned_pids = [
            process.pid
            for process in find_owned_browser_processes(
                owner_pid,
                profile_dir=profile_dir,
            )
        ]
        listing = runner(
            ["wmctrl", "-lp", "-x"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=command_env,
        )
        if listing.returncode == 0:
            window_ids = select_owned_window_ids(
                listing.stdout,
                owned_pids=owned_pids,
                window_class=marker,
            )
        if window_ids:
            break
        time.sleep(0.2)
    if not window_ids:
        result["workspace_move_status"] = "browser_window_not_found"
        return result

    moved: list[str] = []
    errors: list[str] = []
    for window_id in window_ids:
        moved_result = runner(
            [
                "wmctrl",
                "-i",
                "-r",
                window_id,
                "-t",
                str(workspace_index),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=command_env,
        )
        if moved_result.returncode == 0:
            moved.append(window_id)
        else:
            errors.append(
                moved_result.stderr.strip() or f"move failed for {window_id}"
            )
    result.update(
        browser_window_id=moved[0] if moved else None,
        browser_window_ids=moved,
        browser_window_workspace=detected if moved else None,
        workspace_move_status=(
            "moved"
            if moved and not errors
            else "partially_moved"
            if moved
            else "move_failed"
        ),
    )
    if errors:
        result["workspace_move_error"] = "; ".join(errors)
    if log:
        log(
            f"Workspace movement: status={result['workspace_move_status']} "
            f"workspace={detected!r} windows={moved}"
        )
    return result
