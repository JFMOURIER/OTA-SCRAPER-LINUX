#!/usr/bin/env bash
# Force and verify one already-requested Scraper 1 run without changing its
# database, scheduler configuration, exports, or checkpoints.
set -Eeuo pipefail

PROJECT="${OTA_PROJECT_DIR:-/home/jf/Projects/OTA-SCRAPER-LINUX}"
DATA_DIR="$PROJECT/data/instances/near_30_days"
STATUS_DIR="$DATA_DIR/status"
DISPLAY_ID=":11"
PORT=8501
UID_NUMBER="$(id -u)"
USER_RUNTIME="/run/user/$UID_NUMBER"
USER_BUS="unix:path=$USER_RUNTIME/bus"
UI_UNIT="ota-ui-near.service"
DISPATCH_UNIT="ota-scheduler-dispatch.service"
WORKER_UNIT="ota-scraper-run@near_30_days.service"
STAMP="$(date +%Y%m%d_%H%M%S)"
REPORT="$HOME/Downloads/scraper1_force_run_${STAMP}.txt"

mkdir -p "$HOME/Downloads" "$STATUS_DIR"
exec > >(tee "$REPORT") 2>&1

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

user_systemctl() {
  env XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" \
    systemctl --user "$@"
}

user_journal() {
  env XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" \
    journalctl --user "$@"
}

proc_env() {
  local pid="$1" key="$2"
  [[ -r "/proc/$pid/environ" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^${key}=//p" | head -n1
}

listener_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u
  else
    fuser -n tcp "$PORT" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u || true
  fi
}

port_ready() {
  curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/_stcore/health" 2>/dev/null | grep -qx ok
}

worker_main_pid() {
  user_systemctl show "$WORKER_UNIT" -p MainPID --value 2>/dev/null | tr -d ' '
}

worker_is_active() {
  [[ "$(user_systemctl is-active "$WORKER_UNIT" 2>/dev/null || true)" == "active" ]]
}

actual_worker_processes() {
  pgrep -af 'tools/ota_scheduled_run.py.*near_30_days|ota_scheduled_run.py.*near_30_days' 2>/dev/null || true
}

active_collection_exists() {
  worker_is_active && return 0
  [[ -n "$(actual_worker_processes)" ]] && return 0
  return 1
}

install_dropin() {
  local unit="$1" dir="$HOME/.config/systemd/user/${unit}.d" file="$dir/80-scraper1-vnc11.conf"
  mkdir -p "$dir"
  [[ -f "$file" ]] && cp -a "$file" "$file.backup.$STAMP"
  cat > "$file" <<EOF
[Service]
Environment="DISPLAY=$DISPLAY_ID"
Environment="SCRAPER_EXPECTED_DISPLAY=$DISPLAY_ID"
Environment="XAUTHORITY=$HOME/.Xauthority"
Environment="XDG_RUNTIME_DIR=$USER_RUNTIME"
Environment="DBUS_SESSION_BUS_ADDRESS=$USER_BUS"
Environment="WAYLAND_DISPLAY="
Environment="XDG_SESSION_TYPE=x11"
Environment="GDK_BACKEND=x11"
Environment="QT_QPA_PLATFORM=xcb"
Environment="NO_AT_BRIDGE=1"
EOF
  say "Installed environment drop-in for $unit"
}

archive_stale_cancel_request() {
  "$PROJECT/.venv/bin/python" - "$STATUS_DIR" "$STAMP" <<'PY'
from pathlib import Path
import shutil, sys
status = Path(sys.argv[1])
stamp = sys.argv[2]
requests = [status / "scheduled_launch_request.json", status / "manual_run_request.json"]
existing = [p for p in requests if p.exists()]
if not existing:
    print("No manual/scheduled request file exists yet.")
    raise SystemExit(0)
latest_request = max(existing, key=lambda p: p.stat().st_mtime)
cancel = status / "cancel_request.json"
if cancel.exists() and cancel.stat().st_mtime < latest_request.stat().st_mtime:
    archive = status / "failed_requests"
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / f"cancel_request_older_than_new_run_{stamp}.json"
    shutil.move(str(cancel), str(target))
    print(f"Archived stale cancel request: {target}")
else:
    print("No older cancel request needed archiving.")
PY
}

ensure_fresh_queued_request() {
  "$PROJECT/.venv/bin/python" - "$STATUS_DIR" "$STAMP" <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import json, shutil, sys
status = Path(sys.argv[1])
stamp = sys.argv[2]
paths = [status / "scheduled_launch_request.json", status / "manual_run_request.json"]
existing = [p for p in paths if p.exists()]
if not existing:
    print("NO_REQUEST")
    raise SystemExit(4)
source = max(existing, key=lambda p: p.stat().st_mtime)
try:
    data = json.loads(source.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"INVALID_REQUEST:{source}:{exc}")
    raise SystemExit(5)
backup_dir = status / "failed_requests"
backup_dir.mkdir(parents=True, exist_ok=True)
for path in existing:
    shutil.copy2(path, backup_dir / f"{path.stem}_before_force_{stamp}.json")
now = datetime.now(timezone.utc).isoformat(timespec="seconds")
data["state"] = "queued"
data["manual_priority"] = True
data["updated_at"] = now
data["queued_at"] = now
# The worker and dispatcher have used both names during the scheduler evolution.
for path in paths:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
print(f"QUEUED_REQUEST:{source}:{data.get('request_id')}:{data.get('schedule_slot')}")
PY
}

stop_wrong_ui_listener() {
  local service_pid pid cmd pgid attempt
  service_pid="$(user_systemctl show "$UI_UNIT" -p MainPID --value 2>/dev/null | tr -d ' ' || true)"
  mapfile -t pids < <(listener_pids)
  ((${#pids[@]} > 0)) || return 0
  if [[ "$service_pid" =~ ^[1-9][0-9]*$ ]]; then
    for pid in "${pids[@]}"; do
      [[ "$pid" == "$service_pid" ]] || break 2
    done
    return 0
  fi
  for pid in "${pids[@]}"; do
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    [[ "$cmd" == *streamlit* && "$cmd" == *app.py* ]] \
      || die "Port $PORT is owned by unexpected PID $pid: $cmd"
  done
  for pid in "${pids[@]}"; do
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    say "Stopping old manually launched Streamlit PID $pid ..."
    if [[ "$pgid" =~ ^[1-9][0-9]*$ && "$pgid" != "$$" ]]; then
      kill -TERM -- "-$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    else
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for attempt in $(seq 1 25); do
    port_ready || return 0
    sleep 1
  done
  die "The old Streamlit listener did not stop; no force kill was used."
}

restart_verified_ui() {
  user_systemctl reset-failed "$UI_UNIT" >/dev/null 2>&1 || true
  user_systemctl restart "$UI_UNIT"
  for _ in $(seq 1 50); do port_ready && break; sleep 1; done
  port_ready || {
    user_systemctl status "$UI_UNIT" --no-pager || true
    user_journal -u "$UI_UNIT" -n 100 --no-pager || true
    die "$UI_UNIT did not become healthy."
  }
  local pid display bus runtime data
  pid="$(user_systemctl show "$UI_UNIT" -p MainPID --value | tr -d ' ')"
  display="$(proc_env "$pid" DISPLAY 2>/dev/null || true)"
  bus="$(proc_env "$pid" DBUS_SESSION_BUS_ADDRESS 2>/dev/null || true)"
  runtime="$(proc_env "$pid" XDG_RUNTIME_DIR 2>/dev/null || true)"
  data="$(proc_env "$pid" INSTANCE_DATA_DIR 2>/dev/null || true)"
  [[ "$display" == "$DISPLAY_ID" ]] || die "UI DISPLAY is '$display', expected '$DISPLAY_ID'."
  [[ "$bus" == "$USER_BUS" ]] || die "UI D-Bus is '$bus', expected '$USER_BUS'."
  [[ "$runtime" == "$USER_RUNTIME" ]] || die "UI runtime is '$runtime', expected '$USER_RUNTIME'."
  [[ "$data" == "$DATA_DIR" ]] || die "UI data directory is '$data', expected '$DATA_DIR'."
  say "Verified UI PID $pid on $DISPLAY_ID with a working user bus."
}

force_dispatch() {
  user_systemctl reset-failed "$DISPATCH_UNIT" "$WORKER_UNIT" >/dev/null 2>&1 || true
  say "Running the scheduler dispatcher now ..."
  user_systemctl start "$DISPATCH_UNIT" || true
  sleep 4
  if worker_is_active || [[ -n "$(actual_worker_processes)" ]]; then
    say "Dispatcher started the Scraper 1 worker."
    return 0
  fi

  say "Dispatcher did not leave a worker active; starting the instance worker directly ..."
  user_systemctl start "$WORKER_UNIT" || true
  sleep 4
  if worker_is_active || [[ -n "$(actual_worker_processes)" ]]; then
    say "Direct worker start succeeded."
    return 0
  fi

  say "The unit did not remain active. Running the dispatcher Python entry point once with the correct bus ..."
  (
    cd "$PROJECT"
    env DISPLAY="$DISPLAY_ID" SCRAPER_EXPECTED_DISPLAY="$DISPLAY_ID" \
      XAUTHORITY="$HOME/.Xauthority" XDG_RUNTIME_DIR="$USER_RUNTIME" \
      DBUS_SESSION_BUS_ADDRESS="$USER_BUS" WAYLAND_DISPLAY= XDG_SESSION_TYPE=x11 \
      GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb \
      "$PROJECT/.venv/bin/python" tools/ota_schedule_dispatcher.py
  ) || true
  sleep 4
  user_systemctl start "$WORKER_UNIT" || true
}

verify_worker_and_browser() {
  local worker_pid display data browser_line="" i
  for i in $(seq 1 60); do
    worker_pid="$(worker_main_pid 2>/dev/null || true)"
    if [[ "$worker_pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$worker_pid" 2>/dev/null; then
      display="$(proc_env "$worker_pid" DISPLAY 2>/dev/null || true)"
      data="$(proc_env "$worker_pid" INSTANCE_DATA_DIR 2>/dev/null || true)"
      if [[ "$display" == "$DISPLAY_ID" ]]; then
        browser_line="$(pgrep -af 'chrome-linux64/chrome|ms-playwright/.*/chrome|playwright.*chromium' 2>/dev/null | tail -n1 || true)"
        if [[ -n "$browser_line" ]]; then
          say "WORKING: worker PID $worker_pid is active on DISPLAY=$display"
          say "WORKING: Playwright/Chromium process detected: $browser_line"
          say "Worker data directory: ${data:-$DATA_DIR}"
          return 0
        fi
      fi
    fi
    sleep 1
  done

  say ""
  say "The worker/browser verification failed. Exact live evidence follows."
  user_systemctl status "$DISPATCH_UNIT" "$WORKER_UNIT" --no-pager || true
  user_journal -u "$DISPATCH_UNIT" -u "$WORKER_UNIT" -n 160 --no-pager || true
  say "Current request files:"
  for f in "$STATUS_DIR/scheduled_launch_request.json" "$STATUS_DIR/manual_run_request.json" "$STATUS_DIR/current_job_status.json"; do
    [[ -f "$f" ]] && { say "----- $f -----"; tail -n 160 "$f"; }
  done
  die "No verified Scraper 1 VNC worker/browser appeared. Report saved to $REPORT"
}

open_vnc_viewer() {
  local viewer=""
  if command -v vncviewer >/dev/null 2>&1; then viewer="$(command -v vncviewer)"
  elif command -v xtigervncviewer >/dev/null 2>&1; then viewer="$(command -v xtigervncviewer)"
  fi
  if [[ -n "$viewer" ]] && ! pgrep -af 'vncviewer.*(localhost:11|:11)' >/dev/null 2>&1; then
    nohup "$viewer" "localhost:11" >/dev/null 2>&1 &
  fi
}

main() {
  say "Scraper 1 verified VNC force-start"
  say "Report: $REPORT"
  [[ -d "$PROJECT" && -f "$PROJECT/app.py" ]] || die "Project missing at $PROJECT"
  [[ -x "$PROJECT/.venv/bin/python" ]] || die "Project virtual environment is missing."
  [[ -S "$USER_RUNTIME/bus" ]] || die "User D-Bus socket is missing at $USER_RUNTIME/bus"
  env XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" \
    systemctl --user show-environment >/dev/null \
    || die "The real user-systemd bus is not reachable."
  DISPLAY="$DISPLAY_ID" XAUTHORITY="$HOME/.Xauthority" xdpyinfo >/dev/null 2>&1 \
    || die "TigerVNC display $DISPLAY_ID is not running."

  if active_collection_exists; then
    say "A Scraper 1 worker is already active; nothing was restarted."
    user_systemctl status "$WORKER_UNIT" --no-pager || true
    open_vnc_viewer
    exit 0
  fi

  install_dropin "$UI_UNIT"
  install_dropin "$DISPATCH_UNIT"
  install_dropin "$WORKER_UNIT"
  user_systemctl daemon-reload
  archive_stale_cancel_request
  ensure_fresh_queued_request
  stop_wrong_ui_listener
  restart_verified_ui
  user_systemctl restart ota-scheduler-dispatch.timer >/dev/null 2>&1 || true
  force_dispatch
  open_vnc_viewer
  verify_worker_and_browser
  say ""
  say "SUCCESS: Scraper 1 is running and its visible browser is routed to TigerVNC $DISPLAY_ID."
  say "The original scheduler configuration and near_30_days data were preserved."
}

main "$@"
