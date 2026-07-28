#!/usr/bin/env bash
# Enforce process-level VNC containment for the original near_30_days Scraper 1.
# This preserves the database, scheduler configuration, exports, checkpoints,
# and browser profiles. It refuses to modify services while a scrape is active.
set -Eeuo pipefail

PROJECT="${OTA_PROJECT_DIR:-/home/jf/Projects/OTA-SCRAPER-LINUX}"
DATA_DIR="$PROJECT/data/instances/near_30_days"
STATUS_DIR="$DATA_DIR/status"
DISPLAY_ID=":11"
VNC_NUMBER="11"
STREAMLIT_PORT="8501"
USER_ID="$(id -u)"
USER_RUNTIME="/run/user/$USER_ID"
USER_BUS="unix:path=$USER_RUNTIME/bus"
XAUTHORITY_FILE="$HOME/.Xauthority"
GUARD_DIR="$PROJECT/vnc_containment_guard"
UI_UNIT="ota-ui-near.service"
DISPATCH_UNIT="ota-scheduler-dispatch.service"
WORKER_UNIT="ota-scraper-run@near_30_days.service"
LEGACY_WORKER_UNIT="ota-scraper-near.service"
STAMP="$(date +%Y%m%d_%H%M%S)"
REPORT="$HOME/Downloads/scraper1_vnc_containment_${STAMP}.txt"
LAUNCHER="$PROJECT/open_scraper1_vnc_fixed.sh"

mkdir -p "$HOME/Downloads" "$STATUS_DIR"
exec > >(tee "$REPORT") 2>&1

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

user_systemctl() {
  env XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" \
    systemctl --user "$@"
}

proc_env() {
  local pid="$1"
  local key="$2"
  [[ -r "/proc/$pid/environ" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^${key}=//p" | head -n 1
}

worker_processes() {
  pgrep -af 'tools/ota_scheduled_run.py.*near_30_days|ota_scheduled_run.py.*near_30_days' 2>/dev/null || true
}

fresh_active_status() {
  local status_file="$STATUS_DIR/current_job_status.json"
  [[ -f "$status_file" ]] || return 1
  "$PROJECT/.venv/bin/python" - "$status_file" <<'PY' >/dev/null 2>&1
from datetime import datetime, timezone
import json
import sys

try:
    value = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(1)
status = str(value.get("status") or "").lower()
if status not in {"starting", "running", "stopping"}:
    raise SystemExit(1)
stamp = value.get("last_updated_at") or value.get("timestamp")
if not stamp:
    raise SystemExit(0)
try:
    parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
except Exception:
    raise SystemExit(0)
raise SystemExit(0 if age < 900 else 1)
PY
}

active_scrape_exists() {
  [[ "$(user_systemctl is-active "$WORKER_UNIT" 2>/dev/null || true)" == "active" ]] && return 0
  [[ -n "$(worker_processes)" ]] && return 0
  fresh_active_status && return 0
  return 1
}

verify_prerequisites() {
  [[ -d "$PROJECT" && -f "$PROJECT/app.py" ]] || die "Project not found at $PROJECT"
  [[ -x "$PROJECT/.venv/bin/python" ]] || die "Project virtual environment is missing."
  [[ -d "$DATA_DIR" ]] || die "Original near_30_days data directory is missing: $DATA_DIR"
  [[ -S "$USER_RUNTIME/bus" ]] || die "User D-Bus socket is missing: $USER_RUNTIME/bus"
  command -v xdpyinfo >/dev/null 2>&1 || die "xdpyinfo is missing."
  command -v curl >/dev/null 2>&1 || die "curl is missing."
  DISPLAY="$DISPLAY_ID" XAUTHORITY="$XAUTHORITY_FILE" xdpyinfo >/dev/null 2>&1 \
    || die "TigerVNC display $DISPLAY_ID is not running."
  user_systemctl show-environment >/dev/null 2>&1 \
    || die "The real user-systemd bus is not reachable."
  user_systemctl cat "$UI_UNIT" >/dev/null 2>&1 || die "Missing unit: $UI_UNIT"
  user_systemctl cat "$DISPATCH_UNIT" >/dev/null 2>&1 || die "Missing unit: $DISPATCH_UNIT"
  user_systemctl cat "$WORKER_UNIT" >/dev/null 2>&1 || die "Missing unit: $WORKER_UNIT"
  say "Verified TigerVNC display $DISPLAY_ID and the user-systemd bus."
}

write_python_guard() {
  mkdir -p "$GUARD_DIR"
  cat > "$GUARD_DIR/sitecustomize.py" <<'PY'
"""Process-level X-display guard for the OTA Scraper 1 VNC worker.

Python imports sitecustomize automatically when this directory is on PYTHONPATH.
The guard forces this process and every subprocess onto the configured VNC X
server. It also prevents code from redirecting DISPLAY back to the physical
Cinnamon desktop.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Mapping
from typing import Any

_ENABLED = os.environ.get("OTA_VNC_CONTAINED", "").strip().lower() in {
    "1", "true", "yes", "on"
}
_TARGET = os.environ.get("SCRAPER_EXPECTED_DISPLAY", ":11").strip() or ":11"
_XAUTHORITY = os.environ.get("XAUTHORITY") or os.path.expanduser("~/.Xauthority")


def _force_current_environment() -> None:
    if not _ENABLED:
        return
    # Use the original mapping method because __setitem__ is guarded below.
    original = getattr(os, "_ota_original_environ_setitem", None)
    if original is None:
        original = os._Environ.__setitem__  # type: ignore[attr-defined]
    original(os.environ, "DISPLAY", _TARGET)
    original(os.environ, "SCRAPER_EXPECTED_DISPLAY", _TARGET)
    original(os.environ, "XAUTHORITY", _XAUTHORITY)
    original(os.environ, "WAYLAND_DISPLAY", "")
    original(os.environ, "XDG_SESSION_TYPE", "x11")
    original(os.environ, "GDK_BACKEND", "x11")
    original(os.environ, "QT_QPA_PLATFORM", "xcb")


def _guarded_environment(value: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if value is None else value)
    if _ENABLED:
        env["DISPLAY"] = _TARGET
        env["SCRAPER_EXPECTED_DISPLAY"] = _TARGET
        env["XAUTHORITY"] = _XAUTHORITY
        env["WAYLAND_DISPLAY"] = ""
        env["XDG_SESSION_TYPE"] = "x11"
        env["GDK_BACKEND"] = "x11"
        env["QT_QPA_PLATFORM"] = "xcb"
        env["OTA_DISABLE_WORKSPACE_MOVE"] = "1"
        env["OTA_VNC_CONTAINED"] = "1"
    return env


if _ENABLED:
    # Prevent later project code from changing DISPLAY to :0 or another server.
    _original_environ_setitem = os._Environ.__setitem__  # type: ignore[attr-defined]
    os._ota_original_environ_setitem = _original_environ_setitem  # type: ignore[attr-defined]

    def _guarded_environ_setitem(self: Any, key: str, value: str) -> None:
        if str(key) == "DISPLAY":
            value = _TARGET
        _original_environ_setitem(self, key, value)

    os._Environ.__setitem__ = _guarded_environ_setitem  # type: ignore[attr-defined]

    _original_putenv = os.putenv

    def _guarded_putenv(key: str | bytes, value: str | bytes) -> None:
        key_text = key.decode() if isinstance(key, bytes) else str(key)
        if key_text == "DISPLAY":
            value = _TARGET
        _original_putenv(key, value)

    os.putenv = _guarded_putenv  # type: ignore[assignment]
    _force_current_environment()

    # Force every subprocess, including Playwright's driver, wmctrl, xdotool,
    # rclone, and helper processes, to inherit the VNC display. A helper that
    # explicitly requests DISPLAY=:0 is overridden here.
    _original_popen_init = subprocess.Popen.__init__

    def _guarded_popen_init(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
        kwargs["env"] = _guarded_environment(kwargs.get("env"))
        _original_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _guarded_popen_init  # type: ignore[assignment]

    _original_async_exec = asyncio.create_subprocess_exec
    _original_async_shell = asyncio.create_subprocess_shell

    async def _guarded_async_exec(*args: Any, **kwargs: Any):
        kwargs["env"] = _guarded_environment(kwargs.get("env"))
        return await _original_async_exec(*args, **kwargs)

    async def _guarded_async_shell(*args: Any, **kwargs: Any):
        kwargs["env"] = _guarded_environment(kwargs.get("env"))
        return await _original_async_shell(*args, **kwargs)

    asyncio.create_subprocess_exec = _guarded_async_exec  # type: ignore[assignment]
    asyncio.create_subprocess_shell = _guarded_async_shell  # type: ignore[assignment]
PY

  "$PROJECT/.venv/bin/python" -m py_compile "$GUARD_DIR/sitecustomize.py"
  say "Installed and syntax-checked the Python VNC containment guard."
}

write_dropin() {
  local unit="$1"
  local mode="$2"
  local directory="$HOME/.config/systemd/user/${unit}.d"
  local file="$directory/99-vnc-containment.conf"
  mkdir -p "$directory"
  if [[ -f "$file" ]]; then
    cp -a "$file" "$file.backup.$STAMP"
  fi

  if [[ "$mode" == "vnc" ]]; then
    cat > "$file" <<EOF
[Service]
Environment="DISPLAY=$DISPLAY_ID"
Environment="SCRAPER_EXPECTED_DISPLAY=$DISPLAY_ID"
Environment="OTA_VNC_CONTAINED=1"
Environment="OTA_DISABLE_WORKSPACE_MOVE=1"
Environment="XAUTHORITY=$XAUTHORITY_FILE"
Environment="XDG_RUNTIME_DIR=$USER_RUNTIME"
Environment="DBUS_SESSION_BUS_ADDRESS=$USER_BUS"
Environment="WAYLAND_DISPLAY="
Environment="XDG_SESSION_TYPE=x11"
Environment="GDK_BACKEND=x11"
Environment="QT_QPA_PLATFORM=xcb"
Environment="PYTHONPATH=$GUARD_DIR:$PROJECT"
EOF
  else
    cat > "$file" <<EOF
[Service]
Environment="XDG_RUNTIME_DIR=$USER_RUNTIME"
Environment="DBUS_SESSION_BUS_ADDRESS=$USER_BUS"
EOF
  fi
  say "Installed $mode environment for $unit"
}

install_service_guards() {
  write_dropin "$UI_UNIT" "vnc"
  write_dropin "$WORKER_UNIT" "vnc"
  write_dropin "$DISPATCH_UNIT" "bus"
  if user_systemctl cat "$LEGACY_WORKER_UNIT" >/dev/null 2>&1; then
    write_dropin "$LEGACY_WORKER_UNIT" "vnc"
  fi
  user_systemctl daemon-reload

  local ui_env
  local worker_env
  ui_env="$(user_systemctl show "$UI_UNIT" -p Environment --value 2>/dev/null || true)"
  worker_env="$(user_systemctl show "$WORKER_UNIT" -p Environment --value 2>/dev/null || true)"
  [[ "$ui_env" == *"DISPLAY=$DISPLAY_ID"* ]] || die "$UI_UNIT did not load DISPLAY=$DISPLAY_ID"
  [[ "$ui_env" == *"OTA_VNC_CONTAINED=1"* ]] || die "$UI_UNIT did not load the containment guard."
  [[ "$worker_env" == *"DISPLAY=$DISPLAY_ID"* ]] || die "$WORKER_UNIT did not load DISPLAY=$DISPLAY_ID"
  [[ "$worker_env" == *"OTA_VNC_CONTAINED=1"* ]] || die "$WORKER_UNIT did not load the containment guard."
  [[ "$worker_env" == *"PYTHONPATH=$GUARD_DIR:$PROJECT"* ]] || die "$WORKER_UNIT did not load the guard PYTHONPATH."
  say "Verified the UI and worker systemd environments."
}

test_guard() {
  local output
  output="$(
    env \
      DISPLAY=:0 \
      SCRAPER_EXPECTED_DISPLAY="$DISPLAY_ID" \
      OTA_VNC_CONTAINED=1 \
      XAUTHORITY="$XAUTHORITY_FILE" \
      PYTHONPATH="$GUARD_DIR:$PROJECT" \
      "$PROJECT/.venv/bin/python" - <<'PY'
import os
import subprocess

assert os.environ["DISPLAY"] == ":11", os.environ.get("DISPLAY")
os.environ["DISPLAY"] = ":0"
assert os.environ["DISPLAY"] == ":11", os.environ.get("DISPLAY")
child = subprocess.run(
    ["/bin/bash", "-lc", "printf %s \"$DISPLAY\""],
    capture_output=True,
    text=True,
    check=True,
    env={**os.environ, "DISPLAY": ":0"},
)
assert child.stdout == ":11", child.stdout
print("GUARD_OK parent=:11 child=:11")
PY
  )"
  [[ "$output" == *"GUARD_OK parent=:11 child=:11"* ]] || die "The containment self-test failed: $output"
  say "$output"
}

restart_services_safely() {
  active_scrape_exists && die "An active Scraper 1 collection is detected. Press Stop, wait for it to finish, and run this same command again. Nothing was restarted."

  user_systemctl stop ota-scheduler-dispatch.timer >/dev/null 2>&1 || true
  user_systemctl reset-failed "$UI_UNIT" "$WORKER_UNIT" "$DISPATCH_UNIT" >/dev/null 2>&1 || true
  user_systemctl restart "$UI_UNIT"

  local attempt
  for attempt in $(seq 1 50); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${STREAMLIT_PORT}/_stcore/health" 2>/dev/null | grep -qx ok; then
      break
    fi
    sleep 1
  done
  curl -fsS --max-time 3 "http://127.0.0.1:${STREAMLIT_PORT}/_stcore/health" 2>/dev/null | grep -qx ok \
    || die "$UI_UNIT did not become healthy."

  local pid
  local display_value
  local guard_value
  local data_value
  pid="$(user_systemctl show "$UI_UNIT" -p MainPID --value | tr -d ' ')"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || die "Could not determine the Streamlit service PID."
  display_value="$(proc_env "$pid" DISPLAY 2>/dev/null || true)"
  guard_value="$(proc_env "$pid" OTA_VNC_CONTAINED 2>/dev/null || true)"
  data_value="$(proc_env "$pid" INSTANCE_DATA_DIR 2>/dev/null || true)"
  [[ "$display_value" == "$DISPLAY_ID" ]] || die "UI DISPLAY is '$display_value', expected '$DISPLAY_ID'."
  [[ "$guard_value" == "1" ]] || die "UI containment guard is not active."
  [[ "$data_value" == "$DATA_DIR" ]] || die "UI data directory is '$data_value', expected '$DATA_DIR'."

  user_systemctl restart ota-scheduler-dispatch.timer
  [[ "$(user_systemctl is-active ota-scheduler-dispatch.timer 2>/dev/null || true)" == "active" ]] \
    || die "The scheduler dispatch timer did not become active."
  say "Verified Streamlit PID $pid on DISPLAY=$display_value with containment enabled."
}

stop_obsolete_vnc1() {
  # :1 was the earlier experimental Scraper 1 VNC. The working installation is
  # now :11. Stop only the old profile/session, never an arbitrary process.
  if pgrep -af -- '--user-data-dir=/home/jf/.chrome-vnc-scraper1' >/dev/null 2>&1; then
    while read -r pid; do
      [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
      local display_value
      display_value="$(proc_env "$pid" DISPLAY 2>/dev/null || true)"
      if [[ "$display_value" == ":1" || -z "$display_value" ]]; then
        kill -TERM "$pid" 2>/dev/null || true
      fi
    done < <(pgrep -f -- '--user-data-dir=/home/jf/.chrome-vnc-scraper1' 2>/dev/null || true)
  fi

  if command -v tigervncserver >/dev/null 2>&1; then
    tigervncserver -kill :1 >/dev/null 2>&1 || true
  elif command -v vncserver >/dev/null 2>&1; then
    vncserver -kill :1 >/dev/null 2>&1 || true
  fi
  say "Removed the obsolete experimental :1 Scraper 1 VNC session if it was still running."
}

write_launcher() {
  cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
DISPLAY_ID="$DISPLAY_ID"
VNC_NUMBER="$VNC_NUMBER"
PORT="$STREAMLIT_PORT"
USER_RUNTIME="$USER_RUNTIME"
USER_BUS="$USER_BUS"
XAUTHORITY_FILE="$XAUTHORITY_FILE"
UI_UNIT="$UI_UNIT"
PROFILE="$DATA_DIR/control_browser_profile_vnc11"
XSTARTUP="$HOME/.vnc/xstartup-ota-scrapers"

user_systemctl() {
  env XDG_RUNTIME_DIR="\$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="\$USER_BUS" systemctl --user "\$@"
}

display_ready() {
  DISPLAY="\$DISPLAY_ID" XAUTHORITY="\$XAUTHORITY_FILE" xdpyinfo >/dev/null 2>&1
}

if ! display_ready; then
  if command -v tigervncserver >/dev/null 2>&1; then SERVER="\$(command -v tigervncserver)"; else SERVER="\$(command -v vncserver)"; fi
  "\$SERVER" "\$DISPLAY_ID" -localhost yes -geometry 1600x900 -depth 24 -desktop "SCRAPER 1 VNC" -xstartup "\$XSTARTUP"
  for _ in \$(seq 1 30); do display_ready && break; sleep 1; done
fi

display_ready || { echo "TigerVNC \$DISPLAY_ID failed to start." >&2; exit 1; }
user_systemctl start "\$UI_UNIT"
for _ in \$(seq 1 40); do
  curl -fsS --max-time 2 "http://127.0.0.1:\$PORT/_stcore/health" 2>/dev/null | grep -qx ok && break
  sleep 1
done

if command -v vncviewer >/dev/null 2>&1; then VIEWER="\$(command -v vncviewer)"; else VIEWER="\$(command -v xtigervncviewer)"; fi
if ! pgrep -af 'vncviewer.*(localhost:11|:11)' >/dev/null 2>&1; then
  nohup "\$VIEWER" "localhost:\$VNC_NUMBER" >/dev/null 2>&1 &
fi

mkdir -p "\$PROFILE"
if ! pgrep -af -- "--user-data-dir=\$PROFILE" >/dev/null 2>&1; then
  BROWSER=""
  for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "\$candidate" >/dev/null 2>&1; then BROWSER="\$(command -v "\$candidate")"; break; fi
  done
  if [[ -n "\$BROWSER" ]]; then
    nohup env DISPLAY="\$DISPLAY_ID" SCRAPER_EXPECTED_DISPLAY="\$DISPLAY_ID" \
      OTA_VNC_CONTAINED=1 OTA_DISABLE_WORKSPACE_MOVE=1 \
      XAUTHORITY="\$XAUTHORITY_FILE" XDG_RUNTIME_DIR="\$USER_RUNTIME" \
      DBUS_SESSION_BUS_ADDRESS="\$USER_BUS" WAYLAND_DISPLAY= XDG_SESSION_TYPE=x11 \
      GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb \
      "\$BROWSER" --user-data-dir="\$PROFILE" --new-window "http://127.0.0.1:\$PORT" \
      --no-first-run --no-default-browser-check --ozone-platform=x11 >/dev/null 2>&1 &
  fi
fi
EOF
  chmod 700 "$LAUNCHER"

  local desktop_dir
  local desktop_file
  desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
  [[ -n "$desktop_dir" ]] || desktop_dir="$HOME/Desktop"
  mkdir -p "$desktop_dir"
  desktop_file="$desktop_dir/Scraper 1 VNC.desktop"
  [[ -f "$desktop_file" ]] && cp -a "$desktop_file" "$desktop_file.backup.$STAMP"
  cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Scraper 1 VNC
Comment=Open the contained near_30_days scraper on TigerVNC display :11
Exec=$LAUNCHER
Icon=utilities-terminal
Terminal=false
Categories=Development;Network;
StartupNotify=true
EOF
  chmod +x "$desktop_file"
  command -v gio >/dev/null 2>&1 \
    && gio set "$desktop_file" metadata::trusted true >/dev/null 2>&1 || true
  say "Updated the permanent Scraper 1 VNC shortcut."
}

open_viewer() {
  local viewer=""
  if command -v vncviewer >/dev/null 2>&1; then
    viewer="$(command -v vncviewer)"
  elif command -v xtigervncviewer >/dev/null 2>&1; then
    viewer="$(command -v xtigervncviewer)"
  fi
  if [[ -n "$viewer" ]] && ! pgrep -af 'vncviewer.*(localhost:11|:11)' >/dev/null 2>&1; then
    nohup "$viewer" "localhost:$VNC_NUMBER" >/dev/null 2>&1 &
  fi
}

main() {
  say "Scraper 1 TigerVNC containment repair v8"
  say "Report: $REPORT"
  verify_prerequisites

  if active_scrape_exists; then
    die "An active Scraper 1 scrape is detected. Press Stop in the interface, wait until it is stopped, and run this same command again. Nothing was changed."
  fi

  write_python_guard
  install_service_guards
  test_guard
  restart_services_safely
  stop_obsolete_vnc1
  write_launcher
  open_viewer

  say ""
  say "SUCCESS: Scraper 1 UI and worker are forced to TigerVNC DISPLAY=$DISPLAY_ID."
  say "SUCCESS: child processes cannot change DISPLAY back to the physical desktop."
  say "SUCCESS: workspace commands are confined to the VNC X server, not Cinnamon DISPLAY=:0."
  say "SUCCESS: the original near_30_days database, scheduler and exports were preserved."
  say ""
  say "Refresh the browser inside TigerVNC once and start one small test run."
}

main "$@"
