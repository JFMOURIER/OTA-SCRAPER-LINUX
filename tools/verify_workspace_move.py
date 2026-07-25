#!/usr/bin/env python3
"""Launch one disposable Chromium window and verify exact workspace movement."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from services.playwright_safe import launch_managed_chromium_context
from services.workspace_control import (
    discover_cinnamon_session_environment,
    move_browser_windows_to_workspace,
    resolve_workspace_index,
)


def main() -> int:
    session = discover_cinnamon_session_environment()
    if not session:
        print(
            json.dumps(
                {
                    "workspace_move_status": "cinnamon_x11_not_detected",
                    "error": "No active same-user Cinnamon X11 environment",
                },
                indent=2,
            )
        )
        return 2
    os.environ.update(session)
    browser = None
    context = None
    result: dict[str, object] = {}
    with tempfile.TemporaryDirectory(
        prefix="ota-scraper-1-workspace-probe-"
    ) as profile:
        try:
            with sync_playwright() as playwright:
                browser, context, implementation = (
                    launch_managed_chromium_context(
                        playwright,
                        headless=False,
                        profile_dir=Path(profile),
                        args=[
                            "--disable-dev-shm-usage",
                            "--disable-background-networking",
                            "--class=ota-scraper-instance-1",
                        ],
                        viewport={"width": 900, "height": 700},
                    )
                )
                page = context.new_page()
                try:
                    page.goto(
                        "https://www.booking.com/",
                        wait_until="domcontentloaded",
                        timeout=20_000,
                    )
                except Exception as exc:
                    # Window placement does not depend on site availability.
                    result["navigation_warning"] = str(exc)
                result.update(
                    move_browser_windows_to_workspace(
                        owner_pid=os.getpid(),
                        profile_dir=profile,
                        workspace_name="SCRAPER 1",
                        window_class="ota-scraper-instance-1",
                        headless=False,
                        poll_seconds=8.0,
                        session_env=session,
                    )
                )
                result["browser_implementation"] = implementation
                listing = subprocess.run(
                    ["wmctrl", "-d"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={**os.environ, **session},
                )
                expected_index, _ = resolve_workspace_index(
                    listing.stdout,
                    "SCRAPER 1",
                )
                windows = subprocess.run(
                    ["wmctrl", "-lp", "-x"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={**os.environ, **session},
                )
                observed_desktops = {}
                for line in windows.stdout.splitlines():
                    fields = line.split(None, 6)
                    if len(fields) >= 2 and fields[0] in set(
                        result.get("browser_window_ids") or []
                    ):
                        observed_desktops[fields[0]] = int(fields[1])
                result["expected_workspace_index"] = expected_index
                result["observed_window_workspaces"] = observed_desktops
                result["workspace_verified"] = bool(observed_desktops) and all(
                    value == expected_index
                    for value in observed_desktops.values()
                )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return (
        0
        if result.get("workspace_move_status") == "moved"
        and result.get("workspace_verified")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
