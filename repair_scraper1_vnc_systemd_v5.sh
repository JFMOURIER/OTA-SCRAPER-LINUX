#!/usr/bin/env bash
# Repair Scraper 1 so the original near_30_days UI and its scheduled/manual
# workers use the real user-systemd bus while visible Chromium remains on VNC :11.
set -Eeuo pipefail

PROJECT="${OTA_PROJECT_DIR:-/home/jf/Projects/OTA-SCRAPER-LINUX}"
DATA_DIR="$PROJECT/data/instances/near_30_days"
STATUS_DIR="$DATA_DIR/status"
DISPLAY_ID=":11"
VNC_NUMBER=11
VNC_PORT=5911
STREAMLIT_PORT=8501
UID_NUMBER="$(id -u)"
USER_RUNTIME="/run/user/$UID_NUMBER"
USER_BUS="unix:path=$USER_RUNTIME/bus"
XAUTHORITY_FILE="$HOME/.Xauthority"
XSTARTUP="$HOME/.vnc/xstartup-ota-scrapers"
UI_UNIT="ota-ui-near.service"
WORKER_UNIT="ota-scraper-run@near_30_days.service"
LEGACY_WORKER_UNIT="ota-scraper-near.service"
LAUNCHER="$PROJECT/open_scraper1_vnc_fixed.sh"
STAMP="$(date +%Y%m%d_%H%M%S)"

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

user_systemctl() {
  env XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" \
    systemctl --user "$@"
}

server_command() {
  if command -v tigervncserver >/dev/null 2>&1; then command -v tigervncserver
  elif command -v vncserver >/dev/null 2>&1; then command -v vncserver
  else return 1
  fi
}

viewer_command() {
  if command -v vncviewer >/dev/null 2>&1; then command -v vncviewer
  elif command -v xtigervncviewer >/dev/null 2>&1; then command -v xtigervncviewer
  else return 1
  fi
}

listener_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -t -iTCP:"$STREAMLIT_PORT" -sTCP:LISTEN 2>/dev/null | sort -u
  elif command -v fuser >/dev/null 2>&1; then
    fuser -n tcp "$STREAMLIT_PORT" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u || true
  else
    ss -ltnp "sport = :$STREAMLIT_PORT" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u
  fi
}

port_is_open() {
  python3 - "$STREAMLIT_PORT" <<'PY' >/dev/null 2>&1
import socket, sys
with socket.socket() as sock:
    sock.settimeout(0.4)
    raise SystemExit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

proc_env() {
  local pid="$1" key="$2"
  [[ -r "/proc/$pid/environ" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^${key}=//p" | head -n1
}

proc_command() {
  local pid="$1"
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline"
}

display_is_ready() {
  DISPLAY="$DISPLAY_ID" XAUTHORITY="$XAUTHORITY_FILE" xdpyinfo >/dev/null 2>&1
}

active_collection_reported() {
  local status_file="$STATUS_DIR/current_job_status.json"
  [[ -f "$status_file" ]] || return 1
  "$PROJECT/.venv/bin/python" - "$status_file" <<'PY' >/dev/null 2>&1
from datetime import datetime, timezone
import json, sys
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

make_xstartup_if_needed() {
  [[ -x "$XSTARTUP" ]] && return 0
  mkdir -p "$HOME/.vnc"
  cat > "$XSTARTUP" <<'XSTARTUP_EOF'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
unset WAYLAND_DISPLAY
export XDG_SESSION_TYPE=x11
export GDK_BACKEND=x11
export QT_QPA_PLATFORM=xcb
if command -v startxfce4 >/dev/null 2>&1; then
  if command -v dbus-launch >/dev/null 2>&1; then exec dbus-launch --exit-with-session startxfce4; fi
  exec startxfce4
fi
if command -v openbox-session >/dev/null 2>&1; then exec openbox-session; fi
if command -v cinnamon-session >/dev/null 2>&1; then exec cinnamon-session; fi
command -v xterm >/dev/null 2>&1 && xterm &
exec x-window-manager
XSTARTUP_EOF
  chmod 700 "$XSTARTUP"
}

ensure_vnc() {
  local server attempt owner
  if display_is_ready; then
    say "TigerVNC display $DISPLAY_ID is ready."
    return 0
  fi
  server="$(server_command)" || die "TigerVNC server is not installed."
  make_xstartup_if_needed
  if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$VNC_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    owner="$(lsof -nP -iTCP:"$VNC_PORT" -sTCP:LISTEN 2>/dev/null || true)"
    die "Port $VNC_PORT is occupied but DISPLAY $DISPLAY_ID is unusable. Nothing was killed.\n$owner"
  fi
  say "Starting standalone TigerVNC SCRAPER 1 on $DISPLAY_ID ..."
  "$server" "$DISPLAY_ID" -localhost yes -geometry 1600x900 -depth 24 \
    -desktop "SCRAPER 1" -xstartup "$XSTARTUP"
  for attempt in $(seq 1 30); do
    display_is_ready && { say "TigerVNC display $DISPLAY_ID is ready."; return 0; }
    sleep 1
  done
  die "TigerVNC display $DISPLAY_ID failed to become ready."
}

verify_user_bus() {
  [[ -d "$USER_RUNTIME" ]] || die "User runtime directory is missing: $USER_RUNTIME"
  [[ -S "$USER_RUNTIME/bus" ]] || die "User D-Bus socket is missing: $USER_RUNTIME/bus"
  user_systemctl show-environment >/dev/null 2>&1 \
    || die "The user-systemd bus is not reachable even with the correct runtime directory."

  local test_unit="ota-vnc-bus-test-${UID_NUMBER}-$(date +%s)"
  env XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" \
    systemd-run --user --wait --collect --quiet --unit="$test_unit" /usr/bin/true >/dev/null 2>&1 \
    || die "A harmless user-systemd test unit could not be started."
  say "Verified: user-systemd/D-Bus is reachable at $USER_BUS."
}

archive_failed_bus_requests() {
  "$PROJECT/.venv/bin/python" - "$STATUS_DIR" "$STAMP" <<'PY'
from __future__ import annotations
import json
from pathlib import Path
import shutil
import sys

status_dir = Path(sys.argv[1])
stamp = sys.argv[2]
status_path = status_dir / "current_job_status.json"
try:
    status = json.loads(status_path.read_text(encoding="utf-8"))
except Exception:
    status = {}
message = " ".join(
    str(status.get(key) or "")
    for key in ("status", "current_message", "last_error")
).lower()
if "failed_to_start" not in message or "connect to bus" not in message:
    print("No failed D-Bus Run request needed archiving.")
    raise SystemExit(0)

archive_dir = status_dir / "failed_requests"
archive_dir.mkdir(parents=True, exist_ok=True)
moved = []
for name in ("scheduled_launch_request.json", "manual_run_request.json"):
    source = status_dir / name
    if not source.exists():
        continue
    destination = archive_dir / f"{source.stem}_failed_bus_{stamp}{source.suffix}"
    shutil.move(str(source), str(destination))
    moved.append(str(destination))
if moved:
    print("Archived the two stale failed Run request files; a fresh click will create a clean request:")
    for item in moved:
        print(f"  {item}")
else:
    print("No stale request files were present.")
PY
}

write_unit_dropin() {
  local unit="$1" directory="$2" file="$directory/50-vnc11.conf"
  if ! user_systemctl cat "$unit" >/dev/null 2>&1; then
    warn "User unit $unit was not found; no drop-in was written for it."
    return 0
  fi
  mkdir -p "$directory"
  if [[ -f "$file" ]]; then
    cp -a "$file" "$file.backup.$STAMP"
  fi
  cat > "$file" <<EOF
[Service]
Environment="DISPLAY=$DISPLAY_ID"
Environment="SCRAPER_EXPECTED_DISPLAY=$DISPLAY_ID"
Environment="XAUTHORITY=$XAUTHORITY_FILE"
Environment="XDG_RUNTIME_DIR=$USER_RUNTIME"
Environment="DBUS_SESSION_BUS_ADDRESS=$USER_BUS"
Environment="WAYLAND_DISPLAY="
Environment="XDG_SESSION_TYPE=x11"
Environment="GDK_BACKEND=x11"
Environment="QT_QPA_PLATFORM=xcb"
Environment="NO_AT_BRIDGE=1"
EOF
  say "Installed VNC/D-Bus environment for $unit"
}

install_systemd_routing() {
  write_unit_dropin "$UI_UNIT" "$HOME/.config/systemd/user/${UI_UNIT}.d"
  write_unit_dropin "$WORKER_UNIT" "$HOME/.config/systemd/user/${WORKER_UNIT}.d"
  write_unit_dropin "$LEGACY_WORKER_UNIT" "$HOME/.config/systemd/user/${LEGACY_WORKER_UNIT}.d"
  user_systemctl daemon-reload

  local worker_environment
  worker_environment="$(user_systemctl show "$WORKER_UNIT" -p Environment --value 2>/dev/null || true)"
  if [[ -n "$worker_environment" && "$worker_environment" != *"DISPLAY=$DISPLAY_ID"* ]]; then
    die "$WORKER_UNIT did not load DISPLAY=$DISPLAY_ID after daemon-reload."
  fi
  say "Verified: Scraper 1 worker unit is routed to VNC $DISPLAY_ID."
}

stop_wrong_or_manual_ui() {
  local pid command pgid attempt
  active_collection_reported && die "Scraper 1 reports an active collection. Stop it in the interface before repairing. Nothing was changed."

  user_systemctl stop "$UI_UNIT" >/dev/null 2>&1 || true
  mapfile -t pids < <(listener_pids)
  ((${#pids[@]} > 0)) || { say "Port $STREAMLIT_PORT is already free."; return 0; }

  for pid in "${pids[@]}"; do
    command="$(proc_command "$pid" 2>/dev/null || true)"
    if [[ "$command" != *streamlit* || "$command" != *app.py* ]]; then
      die "Port $STREAMLIT_PORT is owned by an unexpected process PID $pid. It was not stopped: $command"
    fi
  done

  for pid in "${pids[@]}"; do
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    say "Stopping manually launched Streamlit PID $pid ..."
    if [[ "$pgid" =~ ^[0-9]+$ && "$pgid" != "$$" ]]; then
      kill -TERM -- "-$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    else
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for attempt in $(seq 1 25); do
    port_is_open || { say "Port $STREAMLIT_PORT is free."; return 0; }
    sleep 1
  done
  die "Port $STREAMLIT_PORT did not close after 25 seconds; no force kill was used."
}

start_systemd_ui() {
  local attempt pid actual_display actual_id actual_data actual_runtime actual_bus
  user_systemctl reset-failed "$UI_UNIT" >/dev/null 2>&1 || true
  user_systemctl start "$UI_UNIT"

  for attempt in $(seq 1 50); do
    if port_is_open; then break; fi
    if [[ "$(user_systemctl is-failed "$UI_UNIT" 2>/dev/null || true)" == "failed" ]]; then
      user_systemctl status "$UI_UNIT" --no-pager >&2 || true
      env XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" \
        journalctl --user -u "$UI_UNIT" -n 100 --no-pager >&2 || true
      die "$UI_UNIT failed during startup."
    fi
    sleep 1
  done
  port_is_open || {
    user_systemctl status "$UI_UNIT" --no-pager >&2 || true
    die "$UI_UNIT did not open port $STREAMLIT_PORT."
  }

  mapfile -t pids < <(listener_pids)
  ((${#pids[@]} > 0)) || die "Port $STREAMLIT_PORT is open but its listener PID could not be identified."
  pid="${pids[0]}"
  actual_display="$(proc_env "$pid" DISPLAY 2>/dev/null || true)"
  actual_id="$(proc_env "$pid" INSTANCE_ID 2>/dev/null || true)"
  actual_data="$(proc_env "$pid" INSTANCE_DATA_DIR 2>/dev/null || true)"
  actual_runtime="$(proc_env "$pid" XDG_RUNTIME_DIR 2>/dev/null || true)"
  actual_bus="$(proc_env "$pid" DBUS_SESSION_BUS_ADDRESS 2>/dev/null || true)"

  [[ "$actual_display" == "$DISPLAY_ID" ]] || die "UI DISPLAY is $actual_display, expected $DISPLAY_ID."
  [[ "$actual_id" == "near_30_days" ]] || die "UI INSTANCE_ID is $actual_id, expected near_30_days."
  [[ "$actual_data" == "$DATA_DIR" ]] || die "UI data directory is $actual_data, expected $DATA_DIR."
  [[ "$actual_runtime" == "$USER_RUNTIME" ]] || die "UI XDG_RUNTIME_DIR is $actual_runtime, expected $USER_RUNTIME."
  [[ "$actual_bus" == "$USER_BUS" ]] || die "UI D-Bus address is $actual_bus, expected $USER_BUS."

  curl -fsS --max-time 5 "http://127.0.0.1:${STREAMLIT_PORT}/_stcore/health" | grep -qx 'ok' \
    || die "Streamlit started but its health endpoint did not return ok."

  say "Verified UI PID $pid: DISPLAY=$actual_display"
  say "Verified UI D-Bus: $actual_bus"
  say "Verified original instance: $actual_id at $actual_data"
}

restart_scheduler_timer() {
  if user_systemctl cat ota-scheduler-dispatch.timer >/dev/null 2>&1; then
    user_systemctl restart ota-scheduler-dispatch.timer
    [[ "$(user_systemctl is-active ota-scheduler-dispatch.timer 2>/dev/null || true)" == "active" ]] \
      || die "ota-scheduler-dispatch.timer did not become active."
    say "Scheduler dispatch timer is active."
  else
    warn "ota-scheduler-dispatch.timer was not found. The Run button can still start the worker directly if supported by the app."
  fi
}

write_permanent_launcher() {
  cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
PROJECT="$PROJECT"
DISPLAY_ID="$DISPLAY_ID"
VNC_NUMBER="$VNC_NUMBER"
PORT="$STREAMLIT_PORT"
UID_NUMBER="$UID_NUMBER"
USER_RUNTIME="$USER_RUNTIME"
USER_BUS="$USER_BUS"
XAUTHORITY_FILE="$XAUTHORITY_FILE"
XSTARTUP="$XSTARTUP"
UI_UNIT="$UI_UNIT"
PROFILE="$DATA_DIR/control_browser_profile_vnc11"
ACTION="\${1:-open}"

user_systemctl() {
  env XDG_RUNTIME_DIR="\$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="\$USER_BUS" systemctl --user "\$@"
}

display_ready() {
  DISPLAY="\$DISPLAY_ID" XAUTHORITY="\$XAUTHORITY_FILE" xdpyinfo >/dev/null 2>&1
}

ensure_vnc() {
  if display_ready; then return 0; fi
  local server
  if command -v tigervncserver >/dev/null 2>&1; then server="\$(command -v tigervncserver)"
  elif command -v vncserver >/dev/null 2>&1; then server="\$(command -v vncserver)"
  else echo "TigerVNC server is missing." >&2; exit 1
  fi
  "\$server" "\$DISPLAY_ID" -localhost yes -geometry 1600x900 -depth 24 -desktop "SCRAPER 1" -xstartup "\$XSTARTUP"
  for _ in \$(seq 1 30); do display_ready && return 0; sleep 1; done
  echo "VNC \$DISPLAY_ID failed to start." >&2; exit 1
}

port_ready() {
  curl -fsS --max-time 2 "http://127.0.0.1:\$PORT/_stcore/health" 2>/dev/null | grep -qx ok
}

open_viewer() {
  local viewer=""
  if command -v vncviewer >/dev/null 2>&1; then viewer="\$(command -v vncviewer)"
  elif command -v xtigervncviewer >/dev/null 2>&1; then viewer="\$(command -v xtigervncviewer)"
  fi
  if [[ -n "\$viewer" ]] && ! pgrep -af 'vncviewer.*(localhost:11|:11)' >/dev/null 2>&1; then
    nohup "\$viewer" "localhost:\$VNC_NUMBER" >/dev/null 2>&1 &
  fi
}

open_control_browser() {
  mkdir -p "\$PROFILE"
  if pgrep -af -- "--user-data-dir=\$PROFILE" >/dev/null 2>&1; then return 0; fi
  local browser=""
  for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "\$candidate" >/dev/null 2>&1; then browser="\$(command -v "\$candidate")"; break; fi
  done
  [[ -n "\$browser" ]] || return 0
  nohup env DISPLAY="\$DISPLAY_ID" XAUTHORITY="\$XAUTHORITY_FILE" \
    XDG_RUNTIME_DIR="\$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="\$USER_BUS" \
    WAYLAND_DISPLAY= XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb \
    "\$browser" --user-data-dir="\$PROFILE" --new-window "http://127.0.0.1:\$PORT" \
    --no-first-run --no-default-browser-check --ozone-platform=x11 >/dev/null 2>&1 &
}

case "\$ACTION" in
  open)
    ensure_vnc
    user_systemctl start "\$UI_UNIT"
    for _ in \$(seq 1 40); do port_ready && break; sleep 1; done
    port_ready || { user_systemctl status "\$UI_UNIT" --no-pager; exit 1; }
    open_viewer
    open_control_browser
    ;;
  status)
    user_systemctl status "\$UI_UNIT" --no-pager || true
    printf 'Expected browser display: %s\n' "\$DISPLAY_ID"
    printf 'Control page: http://127.0.0.1:%s\n' "\$PORT"
    ;;
  restart)
    ensure_vnc
    user_systemctl restart "\$UI_UNIT"
    ;;
  *)
    echo "Usage: \$0 [open|status|restart]" >&2
    exit 2
    ;;
esac
EOF
  chmod 700 "$LAUNCHER"

  local desktop_dir desktop_file
  desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
  [[ -n "$desktop_dir" ]] || desktop_dir="$HOME/Desktop"
  mkdir -p "$desktop_dir"
  desktop_file="$desktop_dir/Scraper 1 VNC.desktop"
  if [[ -f "$desktop_file" ]]; then cp -a "$desktop_file" "$desktop_file.backup.$STAMP"; fi
  cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Scraper 1 VNC
Comment=Open the original near_30_days Scraper 1 and its workers in TigerVNC :11
Exec=$LAUNCHER open
Icon=utilities-terminal
Terminal=false
Categories=Development;Network;
StartupNotify=true
EOF
  chmod +x "$desktop_file"
  command -v gio >/dev/null 2>&1 && gio set "$desktop_file" metadata::trusted true >/dev/null 2>&1 || true
  say "Updated desktop shortcut: $desktop_file"
}

open_existing_desktop() {
  local viewer browser profile
  viewer="$(viewer_command 2>/dev/null || true)"
  if [[ -n "$viewer" ]] && ! pgrep -af 'vncviewer.*(localhost:11|:11)' >/dev/null 2>&1; then
    nohup "$viewer" "localhost:$VNC_NUMBER" >/dev/null 2>&1 &
  fi

  profile="$DATA_DIR/control_browser_profile_vnc11"
  if ! pgrep -af -- "--user-data-dir=$profile" >/dev/null 2>&1; then
    browser=""
    for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
      if command -v "$candidate" >/dev/null 2>&1; then browser="$(command -v "$candidate")"; break; fi
    done
    if [[ -n "$browser" ]]; then
      mkdir -p "$profile"
      nohup env DISPLAY="$DISPLAY_ID" XAUTHORITY="$XAUTHORITY_FILE" \
        XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" \
        WAYLAND_DISPLAY= XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb \
        "$browser" --user-data-dir="$profile" --new-window "http://127.0.0.1:$STREAMLIT_PORT" \
        --no-first-run --no-default-browser-check --ozone-platform=x11 >/dev/null 2>&1 &
    fi
  fi
}

main() {
  [[ -d "$PROJECT" && -f "$PROJECT/app.py" ]] || die "OTA project was not found at $PROJECT"
  [[ -x "$PROJECT/.venv/bin/python" ]] || die "Project virtual environment is missing."
  command -v xdpyinfo >/dev/null 2>&1 || die "xdpyinfo is missing (package x11-utils)."
  command -v curl >/dev/null 2>&1 || die "curl is missing."
  [[ -f "$HOME/.vnc/passwd" ]] || die "TigerVNC password is missing. Run vncpasswd once."

  ensure_vnc
  verify_user_bus
  active_collection_reported && die "Scraper 1 reports an active collection. Stop it first; nothing was changed."

  # Pause dispatch briefly so the stale failed request cannot race this repair.
  user_systemctl stop ota-scheduler-dispatch.timer >/dev/null 2>&1 || true
  archive_failed_bus_requests
  install_systemd_routing
  stop_wrong_or_manual_ui
  start_systemd_ui
  restart_scheduler_timer
  write_permanent_launcher
  open_existing_desktop

  say ""
  say "SUCCESS: Scraper 1 is now managed by $UI_UNIT with its real user-systemd bus."
  say "SUCCESS: Manual and scheduled workers are routed to TigerVNC $DISPLAY_ID."
  say "Scheduler data and the original near_30_days database were preserved."
  say ""
  say "In the VNC browser, refresh the page once, then press Run once."
  say "The old failed queued request was archived, so this next click will be a clean request."
}

main "$@"
