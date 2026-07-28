#!/usr/bin/env bash
# Repair the Scraper 1 Run button and scheduler-to-worker path while keeping
# the original near_30_days data and routing visible Chromium to TigerVNC :11.
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
UI_UNIT="ota-ui-near.service"
DISPATCH_UNIT="ota-scheduler-dispatch.service"
WORKER_UNIT="ota-scraper-run@near_30_days.service"
LEGACY_WORKER_UNIT="ota-scraper-near.service"
STAMP="$(date +%Y%m%d_%H%M%S)"
REPORT="$HOME/Downloads/scraper1_vnc_run_button_repair_${STAMP}.txt"
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

proc_command() {
  local pid="$1"
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline"
}

listener_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -t -iTCP:"$STREAMLIT_PORT" -sTCP:LISTEN 2>/dev/null | sort -u
  elif command -v fuser >/dev/null 2>&1; then
    fuser -n tcp "$STREAMLIT_PORT" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u || true
  else
    ss -ltnp "sport = :$STREAMLIT_PORT" 2>/dev/null \
      | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u
  fi
}

port_is_listening() {
  python3 - "$STREAMLIT_PORT" <<'PYPORT' >/dev/null 2>&1
import socket
import sys
with socket.socket() as sock:
    sock.settimeout(0.4)
    raise SystemExit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PYPORT
}

port_is_healthy() {
  curl -fsS --max-time 3 "http://127.0.0.1:${STREAMLIT_PORT}/_stcore/health" 2>/dev/null \
    | grep -qx 'ok'
}

display_is_ready() {
  DISPLAY="$DISPLAY_ID" XAUTHORITY="$XAUTHORITY_FILE" xdpyinfo >/dev/null 2>&1
}

worker_is_active() {
  [[ "$(user_systemctl is-active "$WORKER_UNIT" 2>/dev/null || true)" == "active" ]]
}

worker_process_exists() {
  pgrep -af 'tools/ota_scheduled_run.py.*near_30_days|ota_scheduled_run.py.*near_30_days' \
    >/dev/null 2>&1
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

verify_prerequisites() {
  [[ -d "$PROJECT" && -f "$PROJECT/app.py" ]] || die "Project not found at $PROJECT"
  [[ -x "$PROJECT/.venv/bin/python" ]] || die "Project virtual environment is missing."
  [[ -d "$DATA_DIR" ]] || die "Original near_30_days data directory is missing: $DATA_DIR"
  [[ -S "$USER_RUNTIME/bus" ]] || die "User D-Bus socket is missing: $USER_RUNTIME/bus"
  command -v curl >/dev/null 2>&1 || die "curl is missing."
  command -v xdpyinfo >/dev/null 2>&1 || die "xdpyinfo is missing."
  display_is_ready || die "TigerVNC display $DISPLAY_ID is not running."
  user_systemctl show-environment >/dev/null 2>&1 \
    || die "The real user-systemd bus is not reachable."
  user_systemctl cat "$UI_UNIT" >/dev/null 2>&1 || die "Missing user unit: $UI_UNIT"
  user_systemctl cat "$DISPATCH_UNIT" >/dev/null 2>&1 || die "Missing user unit: $DISPATCH_UNIT"
  user_systemctl cat "$WORKER_UNIT" >/dev/null 2>&1 || die "Missing user unit: $WORKER_UNIT"

  local test_unit="ota-vnc-bus-check-${USER_ID}-${STAMP}"
  env XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" \
    systemd-run --user --wait --collect --quiet --unit="$test_unit" /usr/bin/true >/dev/null 2>&1 \
    || die "A harmless user-systemd test unit could not run."
  say "Verified user-systemd bus: $USER_BUS"
  say "Verified TigerVNC display: $DISPLAY_ID"
}

backup_and_write_dropin() {
  local unit="$1"
  local mode="$2"
  local directory="$HOME/.config/systemd/user/${unit}.d"
  local file="$directory/90-scraper1-vnc11.conf"
  mkdir -p "$directory"
  if [[ -f "$file" ]]; then
    cp -a "$file" "$file.backup.$STAMP"
  fi

  if [[ "$mode" == "vnc" ]]; then
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
  else
    cat > "$file" <<EOF
[Service]
Environment="XDG_RUNTIME_DIR=$USER_RUNTIME"
Environment="DBUS_SESSION_BUS_ADDRESS=$USER_BUS"
EOF
  fi
  say "Installed service environment: $unit ($mode)"
}

install_service_environments() {
  backup_and_write_dropin "$UI_UNIT" "vnc"
  backup_and_write_dropin "$DISPATCH_UNIT" "bus"
  backup_and_write_dropin "$WORKER_UNIT" "vnc"
  if user_systemctl cat "$LEGACY_WORKER_UNIT" >/dev/null 2>&1; then
    backup_and_write_dropin "$LEGACY_WORKER_UNIT" "vnc"
  fi
  user_systemctl daemon-reload

  local ui_environment
  local dispatcher_environment
  local worker_environment
  ui_environment="$(user_systemctl show "$UI_UNIT" -p Environment --value 2>/dev/null || true)"
  dispatcher_environment="$(user_systemctl show "$DISPATCH_UNIT" -p Environment --value 2>/dev/null || true)"
  worker_environment="$(user_systemctl show "$WORKER_UNIT" -p Environment --value 2>/dev/null || true)"

  [[ "$ui_environment" == *"DISPLAY=$DISPLAY_ID"* ]] \
    || die "$UI_UNIT did not load DISPLAY=$DISPLAY_ID"
  [[ "$ui_environment" == *"DBUS_SESSION_BUS_ADDRESS=$USER_BUS"* ]] \
    || die "$UI_UNIT did not load the user D-Bus address."
  [[ "$dispatcher_environment" == *"DBUS_SESSION_BUS_ADDRESS=$USER_BUS"* ]] \
    || die "$DISPATCH_UNIT did not load the user D-Bus address."
  [[ "$worker_environment" == *"DISPLAY=$DISPLAY_ID"* ]] \
    || die "$WORKER_UNIT did not load DISPLAY=$DISPLAY_ID"
  [[ "$worker_environment" == *"DBUS_SESSION_BUS_ADDRESS=$USER_BUS"* ]] \
    || die "$WORKER_UNIT did not load the user D-Bus address."
  say "Verified UI, dispatcher and worker service environments."
}

archive_failed_bus_requests_only() {
  "$PROJECT/.venv/bin/python" - "$STATUS_DIR" "$STAMP" <<'PY'
from __future__ import annotations
import json
from pathlib import Path
import shutil
import sys

status_dir = Path(sys.argv[1])
stamp = sys.argv[2]
status_file = status_dir / "current_job_status.json"
try:
    status = json.loads(status_file.read_text(encoding="utf-8"))
except Exception:
    status = {}
message = " ".join(str(status.get(k) or "") for k in ("status", "current_message", "last_error")).lower()
failed_bus = "failed_to_start" in message and "connect to bus" in message
if not failed_bus:
    print("No failed D-Bus request needed archiving.")
    raise SystemExit(0)
archive = status_dir / "failed_requests"
archive.mkdir(parents=True, exist_ok=True)
for name in ("scheduled_launch_request.json", "manual_run_request.json"):
    source = status_dir / name
    if not source.exists():
        continue
    destination = archive / f"{source.stem}_failed_bus_v7_{stamp}.json"
    shutil.move(str(source), str(destination))
    print(f"Archived stale failed request: {destination}")
PY
}

archive_stale_cancel_request() {
  local cancel_file="$STATUS_DIR/cancel_request.json"
  [[ -f "$cancel_file" ]] || return 0
  local archive_dir="$STATUS_DIR/failed_requests"
  local destination="$archive_dir/cancel_request_before_vnc_repair_${STAMP}.json"
  mkdir -p "$archive_dir"
  mv "$cancel_file" "$destination"
  say "Archived the old stop request: $destination"
}

stop_manual_ui_safely() {
  local pid
  local command
  local data_dir
  local pgid
  local attempt

  if worker_is_active || worker_process_exists || fresh_active_status; then
    die "An active Scraper 1 collection is detected. Nothing was stopped."
  fi

  user_systemctl stop "$UI_UNIT" >/dev/null 2>&1 || true
  mapfile -t pids < <(listener_pids)
  if ((${#pids[@]} == 0)); then
    say "Port $STREAMLIT_PORT is already free."
    return 0
  fi

  for pid in "${pids[@]}"; do
    command="$(proc_command "$pid" 2>/dev/null || true)"
    data_dir="$(proc_env "$pid" INSTANCE_DATA_DIR 2>/dev/null || true)"
    [[ "$command" == *streamlit* && "$command" == *app.py* ]] \
      || die "Unexpected process owns port $STREAMLIT_PORT (PID $pid): $command"
    [[ -z "$data_dir" || "$data_dir" == "$DATA_DIR" ]] \
      || die "Port $STREAMLIT_PORT belongs to a different instance: $data_dir"
  done

  for pid in "${pids[@]}"; do
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    say "Stopping the manually launched Streamlit process PID $pid ..."
    if [[ "$pgid" =~ ^[1-9][0-9]*$ && "$pgid" != "$$" ]]; then
      kill -TERM -- "-$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    else
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done

  for attempt in $(seq 1 25); do
    if ! port_is_listening; then
      say "Port $STREAMLIT_PORT is free."
      return 0
    fi
    sleep 1
  done
  die "Port $STREAMLIT_PORT did not close; no force kill was used."
}

start_and_verify_ui() {
  local attempt
  local pid
  local display_value
  local data_value
  local runtime_value
  local bus_value

  user_systemctl reset-failed "$UI_UNIT" >/dev/null 2>&1 || true
  user_systemctl start "$UI_UNIT"
  for attempt in $(seq 1 50); do
    if port_is_healthy; then
      break
    fi
    if [[ "$(user_systemctl is-failed "$UI_UNIT" 2>/dev/null || true)" == "failed" ]]; then
      user_systemctl status "$UI_UNIT" --no-pager >&2 || true
      env XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" \
        journalctl --user -u "$UI_UNIT" -n 100 --no-pager >&2 || true
      die "$UI_UNIT failed during startup."
    fi
    sleep 1
  done
  port_is_healthy || die "$UI_UNIT did not become healthy on port $STREAMLIT_PORT."

  mapfile -t pids < <(listener_pids)
  ((${#pids[@]} > 0)) || die "The healthy Streamlit listener PID could not be identified."
  pid="${pids[0]}"
  display_value="$(proc_env "$pid" DISPLAY 2>/dev/null || true)"
  data_value="$(proc_env "$pid" INSTANCE_DATA_DIR 2>/dev/null || true)"
  runtime_value="$(proc_env "$pid" XDG_RUNTIME_DIR 2>/dev/null || true)"
  bus_value="$(proc_env "$pid" DBUS_SESSION_BUS_ADDRESS 2>/dev/null || true)"

  [[ "$display_value" == "$DISPLAY_ID" ]] \
    || die "Streamlit DISPLAY is '$display_value', expected '$DISPLAY_ID'."
  [[ "$data_value" == "$DATA_DIR" ]] \
    || die "Streamlit data directory is '$data_value', expected '$DATA_DIR'."
  [[ "$runtime_value" == "$USER_RUNTIME" ]] \
    || die "Streamlit runtime is '$runtime_value', expected '$USER_RUNTIME'."
  [[ "$bus_value" == "$USER_BUS" ]] \
    || die "Streamlit D-Bus is '$bus_value', expected '$USER_BUS'."

  say "Verified Streamlit PID $pid on DISPLAY=$display_value"
  say "Verified original data directory: $data_value"
  say "Verified Run-button D-Bus: $bus_value"
}

restart_scheduler_timer() {
  user_systemctl reset-failed "$DISPATCH_UNIT" "$WORKER_UNIT" >/dev/null 2>&1 || true
  user_systemctl restart ota-scheduler-dispatch.timer
  [[ "$(user_systemctl is-active ota-scheduler-dispatch.timer 2>/dev/null || true)" == "active" ]] \
    || die "The scheduler dispatch timer did not become active."
  say "Scheduler dispatch timer is active."
}

write_launcher_and_shortcut() {
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
  "\$SERVER" "\$DISPLAY_ID" -localhost yes -geometry 1600x900 -depth 24 -desktop "SCRAPER 1" -xstartup "\$XSTARTUP"
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
    nohup env DISPLAY="\$DISPLAY_ID" XAUTHORITY="\$XAUTHORITY_FILE" \
      XDG_RUNTIME_DIR="\$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="\$USER_BUS" \
      WAYLAND_DISPLAY= XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb \
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
  if [[ -f "$desktop_file" ]]; then
    cp -a "$desktop_file" "$desktop_file.backup.$STAMP"
  fi
  cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Scraper 1 VNC
Comment=Open the original near_30_days scraper in isolated TigerVNC :11
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

open_existing_viewer() {
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
  say "Scraper 1 VNC Run-button repair v7"
  say "Report: $REPORT"
  verify_prerequisites

  if worker_is_active || worker_process_exists || fresh_active_status; then
    die "An active Scraper 1 collection is detected. Nothing was changed."
  fi

  user_systemctl stop ota-scheduler-dispatch.timer >/dev/null 2>&1 || true
  archive_failed_bus_requests_only
  archive_stale_cancel_request
  install_service_environments
  stop_manual_ui_safely
  start_and_verify_ui
  restart_scheduler_timer
  write_launcher_and_shortcut
  open_existing_viewer

  say ""
  say "SUCCESS: the Scraper 1 UI now has a working user-systemd bus."
  say "SUCCESS: the near_30_days worker is routed to TigerVNC $DISPLAY_ID."
  say "SUCCESS: the scheduler timer is active and the original data/config were preserved."
  say ""
  say "Inside the VNC browser, press Ctrl+R once and then press Run once."
  say "A fresh Run request should now start ota-scraper-run@near_30_days.service."
}

main "$@"
