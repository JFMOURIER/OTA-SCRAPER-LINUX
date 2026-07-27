#!/usr/bin/env bash
# OTA SCRAPER LINUX — TigerVNC isolation launcher v2
# Installs a guarded launcher for Scrapers 1–3.
# It does not stop a live scraper during installation.
set -Eeuo pipefail

PROJECT_DIR="${OTA_PROJECT_DIR:-/home/jf/Projects/OTA-SCRAPER-LINUX}"
CONTROL_SCRIPT="$PROJECT_DIR/vnc_scraper_control.sh"
VNC_DIR="$HOME/.vnc"
XSTARTUP="$VNC_DIR/xstartup-ota-scrapers"

if [[ ! -d "$PROJECT_DIR" || ! -f "$PROJECT_DIR/app.py" ]]; then
  echo "ERROR: OTA project not found at: $PROJECT_DIR" >&2
  echo "Run with the correct path, for example:" >&2
  echo "  OTA_PROJECT_DIR=/correct/path/OTA-SCRAPER-LINUX $0" >&2
  exit 1
fi

mkdir -p "$VNC_DIR" "$PROJECT_DIR/data/instances"

# Keep a timestamped backup of the previous launcher.
if [[ -f "$CONTROL_SCRIPT" ]]; then
  cp -a "$CONTROL_SCRIPT" "$CONTROL_SCRIPT.backup.$(date +%Y%m%d_%H%M%S)"
fi

cat > "$XSTARTUP" <<'XSTARTUP_EOF'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
unset WAYLAND_DISPLAY
export XDG_SESSION_TYPE=x11
export GDK_BACKEND=x11
export QT_QPA_PLATFORM=xcb

run_session() {
    if command -v dbus-launch >/dev/null 2>&1; then
        exec dbus-launch --exit-with-session "$@"
    fi
    exec "$@"
}

if command -v startxfce4 >/dev/null 2>&1; then
    run_session startxfce4
fi
if command -v openbox-session >/dev/null 2>&1; then
    command -v tint2 >/dev/null 2>&1 && tint2 >/dev/null 2>&1 &
    run_session openbox-session
fi
if command -v cinnamon-session >/dev/null 2>&1; then
    run_session cinnamon-session
fi
command -v xterm >/dev/null 2>&1 && xterm >/dev/null 2>&1 &
if command -v x-window-manager >/dev/null 2>&1; then
    run_session x-window-manager
fi
printf '%s\n' "No usable VNC desktop session was found." >&2
exit 1
XSTARTUP_EOF
chmod 700 "$XSTARTUP"

cat > "$CONTROL_SCRIPT" <<'CONTROL_EOF'
#!/usr/bin/env bash
# Guarded OTA scraper controller for isolated TigerVNC displays.
set -Eeuo pipefail

PROJECT_DIR="__PROJECT_DIR__"
XSTARTUP="$HOME/.vnc/xstartup-ota-scrapers"
VNC_GEOMETRY="${VNC_GEOMETRY:-1600x900}"

usage() {
  cat <<'USAGE'
Usage:
  vnc_scraper_control.sh repair SLOT      Replace an old/wrong-display backend safely
  vnc_scraper_control.sh open SLOT        Start correctly, open VNC viewer and VNC-only control browser
  vnc_scraper_control.sh start SLOT       Start VNC display and correctly bound backend
  vnc_scraper_control.sh browser SLOT     Open the control page inside VNC using an isolated browser profile
  vnc_scraper_control.sh viewer SLOT      Open only the TigerVNC viewer
  vnc_scraper_control.sh diagnose SLOT    Show the real DISPLAY and data directory of the port listener
  vnc_scraper_control.sh status SLOT      Show concise status
  vnc_scraper_control.sh stop SLOT        Stop only the managed backend and its browser children
  vnc_scraper_control.sh restart SLOT     Restart a managed backend on its VNC display
  vnc_scraper_control.sh stop-vnc SLOT    Stop VNC after its backend is stopped
  vnc_scraper_control.sh status-all       Show all three slots

Mapping:
  1 -> DISPLAY=:1 -> port 8501 -> data/instances/scraper_1
  2 -> DISPLAY=:2 -> port 8502 -> data/instances/scraper_2
  3 -> DISPLAY=:3 -> port 8503 -> data/instances/scraper_3
USAGE
}

validate_slot() {
  case "${1:-}" in
    1|2|3) ;;
    *) echo "ERROR: SLOT must be 1, 2 or 3." >&2; usage; exit 2 ;;
  esac
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

slot_port() { echo $((8500 + $1)); }
slot_display() { echo ":$1"; }
slot_data_dir() { echo "$PROJECT_DIR/data/instances/scraper_$1"; }
slot_pidfile() { echo "$(slot_data_dir "$1")/status/streamlit_vnc.pid"; }
slot_pgidfile() { echo "$(slot_data_dir "$1")/status/streamlit_vnc.pgid"; }
slot_logfile() { echo "$(slot_data_dir "$1")/logs/vnc_launcher.log"; }
slot_runtime_dir() { echo "$(slot_data_dir "$1")/xdg_runtime"; }

proc_env() {
  local pid="$1" key="$2"
  [[ -r "/proc/$pid/environ" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^${key}=//p" | head -n 1
}

proc_cmdline() {
  local pid="$1"
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline"
}

listener_pids() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u
    return 0
  fi
  if command -v fuser >/dev/null 2>&1; then
    fuser -n tcp "$port" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u || true
    return 0
  fi
  ss -ltnp "sport = :$port" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u
}

port_is_listening() {
  local port="$1"
  python3 - "$port" <<'PY' >/dev/null 2>&1
import socket, sys
port = int(sys.argv[1])
with socket.socket() as sock:
    sock.settimeout(0.4)
    raise SystemExit(0 if sock.connect_ex(("127.0.0.1", port)) == 0 else 1)
PY
}

pid_is_alive() {
  local pidfile="$1" pid
  [[ -s "$pidfile" ]] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

display_is_ready() {
  local slot="$1" display
  display="$(slot_display "$slot")"
  if command -v xdpyinfo >/dev/null 2>&1; then
    DISPLAY="$display" XAUTHORITY="$HOME/.Xauthority" xdpyinfo >/dev/null 2>&1
    return
  fi
  [[ -S "/tmp/.X11-unix/X$slot" ]]
}

ensure_prerequisites() {
  local missing=0
  server_command >/dev/null 2>&1 || { echo "ERROR: TigerVNC server is missing." >&2; missing=1; }
  viewer_command >/dev/null 2>&1 || { echo "ERROR: TigerVNC viewer is missing." >&2; missing=1; }
  command -v xdpyinfo >/dev/null 2>&1 || { echo "ERROR: xdpyinfo is missing (package x11-utils)." >&2; missing=1; }
  [[ -f "$HOME/.vnc/passwd" ]] || { echo "ERROR: Run vncpasswd once first." >&2; missing=1; }
  [[ -x "$PROJECT_DIR/.venv/bin/streamlit" ]] || { echo "ERROR: $PROJECT_DIR/.venv/bin/streamlit is missing." >&2; missing=1; }
  if [[ "$missing" -ne 0 ]]; then
    echo "Install dependencies with:" >&2
    echo "  sudo apt update && sudo apt install -y tigervnc-standalone-server tigervnc-viewer xfce4 dbus-x11 x11-utils lsof" >&2
    exit 3
  fi
}

ensure_vnc_display() {
  local slot="$1" display server attempt
  display="$(slot_display "$slot")"
  if display_is_ready "$slot"; then
    echo "TigerVNC display $display is ready."
    return 0
  fi
  server="$(server_command)"
  echo "Starting isolated TigerVNC desktop SCRAPER $slot on $display ..."
  "$server" "$display" -localhost yes -geometry "$VNC_GEOMETRY" -depth 24 \
    -desktop "SCRAPER $slot" -xstartup "$XSTARTUP"
  for attempt in $(seq 1 30); do
    display_is_ready "$slot" && { echo "TigerVNC display $display is ready."; return 0; }
    sleep 1
  done
  echo "ERROR: TigerVNC display $display failed to become ready." >&2
  return 4
}

status_file_for_pid() {
  local pid="$1" raw data_dir
  raw="$(proc_env "$pid" INSTANCE_DATA_DIR || true)"
  if [[ -n "$raw" ]]; then
    if [[ "$raw" = /* ]]; then data_dir="$raw"; else data_dir="$PROJECT_DIR/$raw"; fi
  else
    raw="$(sed -n 's/^INSTANCE_DATA_DIR=//p' "$PROJECT_DIR/.env" 2>/dev/null | tail -n 1 || true)"
    if [[ -n "$raw" ]]; then
      if [[ "$raw" = /* ]]; then data_dir="$raw"; else data_dir="$PROJECT_DIR/$raw"; fi
    else
      data_dir="$PROJECT_DIR/data"
    fi
  fi
  echo "$data_dir/status/current_job_status.json"
}

status_file_is_live() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  python3 - "$file" <<'PY'
from datetime import datetime
import json, sys
p = sys.argv[1]
try:
    data = json.load(open(p, encoding="utf-8"))
except Exception:
    raise SystemExit(1)
status = str(data.get("status") or "").lower()
if status not in {"starting", "running"}:
    raise SystemExit(1)
stamp = data.get("last_updated_at") or data.get("timestamp")
if not stamp:
    raise SystemExit(0)
try:
    age = (datetime.now() - datetime.fromisoformat(str(stamp))).total_seconds()
except Exception:
    raise SystemExit(0)
raise SystemExit(0 if age < 600 else 1)
PY
}

show_listener() {
  local slot="$1" port expected pid cmd actual data expected_env
  port="$(slot_port "$slot")"
  expected="$(slot_display "$slot")"
  mapfile -t pids < <(listener_pids "$port")
  if ((${#pids[@]} == 0)); then
    echo "Port $port: no listener"
    return 0
  fi
  for pid in "${pids[@]}"; do
    cmd="$(proc_cmdline "$pid" 2>/dev/null || true)"
    actual="$(proc_env "$pid" DISPLAY 2>/dev/null || true)"
    data="$(proc_env "$pid" INSTANCE_DATA_DIR 2>/dev/null || true)"
    expected_env="$(proc_env "$pid" SCRAPER_EXPECTED_DISPLAY 2>/dev/null || true)"
    echo "Port $port listener PID $pid"
    echo "  command: ${cmd:-unknown}"
    echo "  DISPLAY: ${actual:-not set}"
    echo "  SCRAPER_EXPECTED_DISPLAY: ${expected_env:-not set}"
    echo "  INSTANCE_DATA_DIR: ${data:-not set}"
    if [[ "$actual" == "$expected" ]]; then
      echo "  display check: CORRECT"
    else
      echo "  display check: WRONG — expected $expected"
    fi
  done
}

backend_is_correct() {
  local slot="$1" port expected expected_data pid actual data
  port="$(slot_port "$slot")"
  expected="$(slot_display "$slot")"
  expected_data="$(slot_data_dir "$slot")"
  mapfile -t pids < <(listener_pids "$port")
  ((${#pids[@]} > 0)) || return 1
  for pid in "${pids[@]}"; do
    actual="$(proc_env "$pid" DISPLAY 2>/dev/null || true)"
    data="$(proc_env "$pid" INSTANCE_DATA_DIR 2>/dev/null || true)"
    [[ "$actual" == "$expected" && "$data" == "$expected_data" ]] || return 1
  done
  return 0
}

stop_listener_safely() {
  local slot="$1" port pid cmd status_file pgid attempt
  port="$(slot_port "$slot")"
  mapfile -t pids < <(listener_pids "$port")
  ((${#pids[@]} > 0)) || return 0

  for pid in "${pids[@]}"; do
    cmd="$(proc_cmdline "$pid" 2>/dev/null || true)"
    if [[ "$cmd" != *streamlit* && "$cmd" != *"app.py"* ]]; then
      echo "ERROR: PID $pid on port $port does not look like the OTA Streamlit app." >&2
      echo "It was not stopped: $cmd" >&2
      return 11
    fi
    status_file="$(status_file_for_pid "$pid")"
    if status_file_is_live "$status_file"; then
      cat >&2 <<EOF
ERROR: Scraper $slot appears to have an active collection.
Nothing was killed, so your data remains safe.
Use the Stop button in the Scraper $slot interface, wait until it stops, then run:
  $0 repair $slot
Status file checked:
  $status_file
EOF
      return 12
    fi
  done

  for pid in "${pids[@]}"; do
    echo "Stopping old Streamlit listener PID $pid on port $port ..."
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [[ "$pgid" =~ ^[0-9]+$ && "$pgid" != "$$" ]]; then
      kill -TERM -- "-$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    else
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done

  for attempt in $(seq 1 20); do
    port_is_listening "$port" || return 0
    sleep 1
  done
  echo "ERROR: Port $port is still occupied after 20 seconds; it was not force-killed." >&2
  return 13
}

clean_stale_profile_locks() {
  local slot="$1" profile
  profile="$(slot_data_dir "$slot")/browser_profile"
  mkdir -p "$profile"
  if pgrep -af -- "$profile" >/dev/null 2>&1; then
    echo "A browser still uses $profile; profile locks were left untouched."
    return 0
  fi
  find "$profile" -maxdepth 3 -type l \( -name 'SingletonLock' -o -name 'SingletonCookie' -o -name 'SingletonSocket' \) -delete 2>/dev/null || true
  find "$profile" -maxdepth 3 -type f \( -name 'SingletonLock' -o -name 'SingletonCookie' -o -name 'SingletonSocket' \) -delete 2>/dev/null || true
}

start_backend() {
  local slot="$1" port display data_dir pidfile pgidfile logfile runtime streamlit pid pgid attempt
  port="$(slot_port "$slot")"
  display="$(slot_display "$slot")"
  data_dir="$(slot_data_dir "$slot")"
  pidfile="$(slot_pidfile "$slot")"
  pgidfile="$(slot_pgidfile "$slot")"
  logfile="$(slot_logfile "$slot")"
  runtime="$(slot_runtime_dir "$slot")"
  streamlit="$PROJECT_DIR/.venv/bin/streamlit"

  mkdir -p "$data_dir"/{logs,status,exports,screenshots,debug,checkpoints,partial,browser_profile,control_browser_profile} "$runtime"
  chmod 700 "$runtime"

  if backend_is_correct "$slot"; then
    echo "Scraper $slot is already correctly bound to $display on port $port."
    return 0
  fi
  if port_is_listening "$port"; then
    echo "ERROR: Port $port is occupied by a backend that is not correctly bound to $display." >&2
    echo "Run: $0 diagnose $slot" >&2
    echo "Then, after stopping any active collection, run: $0 repair $slot" >&2
    return 6
  fi
  display_is_ready "$slot" || { echo "ERROR: $display is unavailable; refusing to launch a visible browser." >&2; return 7; }

  clean_stale_profile_locks "$slot"
  rm -f "$pidfile" "$pgidfile"
  echo "Starting Scraper $slot on DISPLAY=$display, port $port ..."
  cd "$PROJECT_DIR"

  setsid env \
    DISPLAY="$display" \
    XAUTHORITY="$HOME/.Xauthority" \
    XDG_RUNTIME_DIR="$runtime" \
    WAYLAND_DISPLAY= \
    XDG_SESSION_TYPE=x11 \
    GDK_BACKEND=x11 \
    QT_QPA_PLATFORM=xcb \
    NO_AT_BRIDGE=1 \
    DBUS_SESSION_BUS_ADDRESS= \
    INSTANCE_ID="scraper_$slot" \
    INSTANCE_NAME="Scraper $slot" \
    INSTANCE_PORT="$port" \
    INSTANCE_DATA_DIR="$data_dir" \
    SCRAPER_EXPECTED_DISPLAY="$display" \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PYTHONUNBUFFERED=1 \
    "$streamlit" run app.py \
      --server.address 127.0.0.1 \
      --server.port "$port" \
      --server.headless true \
      --browser.gatherUsageStats false \
    >>"$logfile" 2>&1 < /dev/null &
  pid=$!
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  echo "$pid" > "$pidfile"
  [[ "$pgid" =~ ^[0-9]+$ ]] && echo "$pgid" > "$pgidfile"

  for attempt in $(seq 1 40); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "ERROR: Scraper $slot exited during startup." >&2
      tail -n 60 "$logfile" >&2 || true
      rm -f "$pidfile" "$pgidfile"
      return 8
    fi
    if port_is_listening "$port"; then
      if backend_is_correct "$slot"; then
        echo "SUCCESS: Scraper $slot is isolated on $display."
        echo "  UI: http://localhost:$port"
        echo "  Data: $data_dir"
        echo "  Log: $logfile"
        return 0
      fi
      echo "ERROR: Port started, but its environment verification failed." >&2
      show_listener "$slot" >&2
      return 9
    fi
    sleep 1
  done
  echo "ERROR: Scraper $slot did not start listening on port $port." >&2
  tail -n 60 "$logfile" >&2 || true
  return 10
}

open_viewer() {
  local slot="$1" viewer
  viewer="$(viewer_command)"
  display_is_ready "$slot" || { echo "ERROR: VNC :$slot is not running." >&2; return 7; }
  nohup "$viewer" "localhost:$slot" >/dev/null 2>&1 &
  echo "Opened TigerVNC viewer SCRAPER $slot."
}

open_control_browser() {
  local slot="$1" port display data_dir runtime profile url browser
  port="$(slot_port "$slot")"
  display="$(slot_display "$slot")"
  data_dir="$(slot_data_dir "$slot")"
  runtime="$(slot_runtime_dir "$slot")"
  profile="$data_dir/control_browser_profile"
  url="http://127.0.0.1:$port"
  mkdir -p "$profile" "$runtime"
  chmod 700 "$runtime"
  display_is_ready "$slot" || { echo "ERROR: $display is unavailable." >&2; return 7; }
  port_is_listening "$port" || { echo "ERROR: Scraper $slot is not listening on port $port." >&2; return 6; }

  browser=""
  for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$candidate" >/dev/null 2>&1; then browser="$(command -v "$candidate")"; break; fi
  done
  if [[ -n "$browser" ]]; then
    nohup env DISPLAY="$display" XAUTHORITY="$HOME/.Xauthority" XDG_RUNTIME_DIR="$runtime" \
      WAYLAND_DISPLAY= XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 DBUS_SESSION_BUS_ADDRESS= \
      "$browser" --user-data-dir="$profile" --new-window "$url" \
      --no-first-run --no-default-browser-check --ozone-platform=x11 \
      >/dev/null 2>&1 &
    echo "Opened an isolated control browser inside VNC $display."
    return 0
  fi

  if command -v firefox >/dev/null 2>&1; then
    nohup env DISPLAY="$display" XAUTHORITY="$HOME/.Xauthority" XDG_RUNTIME_DIR="$runtime" \
      WAYLAND_DISPLAY= XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 DBUS_SESSION_BUS_ADDRESS= \
      firefox --no-remote --profile "$profile/firefox" "$url" >/dev/null 2>&1 &
    echo "Opened Firefox inside VNC $display."
    return 0
  fi
  echo "No Chrome/Chromium/Firefox executable was found. Open $url manually inside VNC." >&2
  return 14
}

stop_managed_backend() {
  local slot="$1" pidfile pgidfile pid pgid attempt
  pidfile="$(slot_pidfile "$slot")"
  pgidfile="$(slot_pgidfile "$slot")"
  if ! pid_is_alive "$pidfile"; then
    echo "No managed Scraper $slot backend is running."
    return 0
  fi
  pid="$(cat "$pidfile")"
  pgid="$(cat "$pgidfile" 2>/dev/null || true)"
  echo "Stopping managed Scraper $slot PID $pid ..."
  if [[ "$pgid" =~ ^[0-9]+$ ]]; then
    kill -TERM -- "-$pgid" 2>/dev/null || true
  else
    kill -TERM "$pid" 2>/dev/null || true
  fi
  for attempt in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || { rm -f "$pidfile" "$pgidfile"; echo "Scraper $slot stopped."; return 0; }
    sleep 1
  done
  echo "Process did not exit; sending SIGKILL to its isolated process group." >&2
  if [[ "$pgid" =~ ^[0-9]+$ ]]; then kill -KILL -- "-$pgid" 2>/dev/null || true; else kill -KILL "$pid" 2>/dev/null || true; fi
  rm -f "$pidfile" "$pgidfile"
}

status_slot() {
  local slot="$1" port display vnc="stopped" backend="stopped"
  port="$(slot_port "$slot")"; display="$(slot_display "$slot")"
  display_is_ready "$slot" && vnc="running"
  if backend_is_correct "$slot"; then backend="running correctly"
  elif port_is_listening "$port"; then backend="WRONG/old listener"
  fi
  echo "Scraper $slot | VNC $display $vnc | port $port | backend: $backend"
}

stop_vnc() {
  local slot="$1" server
  if port_is_listening "$(slot_port "$slot")"; then
    echo "ERROR: Refusing to stop VNC while port $(slot_port "$slot") is active." >&2
    return 10
  fi
  server="$(server_command)"
  "$server" -kill ":$slot" || true
}

command_name="${1:-}"
slot="${2:-}"
case "$command_name" in
  repair)
    validate_slot "$slot"; ensure_prerequisites; ensure_vnc_display "$slot"
    if backend_is_correct "$slot"; then
      echo "No repair needed: Scraper $slot is already correctly isolated."
    else
      show_listener "$slot"
      stop_listener_safely "$slot"
      start_backend "$slot"
    fi
    open_viewer "$slot"
    open_control_browser "$slot"
    ;;
  open)
    validate_slot "$slot"; ensure_prerequisites; ensure_vnc_display "$slot"; start_backend "$slot"; open_viewer "$slot"; open_control_browser "$slot"
    ;;
  start)
    validate_slot "$slot"; ensure_prerequisites; ensure_vnc_display "$slot"; start_backend "$slot"
    ;;
  browser)
    validate_slot "$slot"; open_control_browser "$slot"
    ;;
  viewer)
    validate_slot "$slot"; open_viewer "$slot"
    ;;
  diagnose)
    validate_slot "$slot"; status_slot "$slot"; show_listener "$slot"
    ;;
  status)
    validate_slot "$slot"; status_slot "$slot"
    ;;
  status-all)
    for slot in 1 2 3; do status_slot "$slot"; done
    ;;
  stop)
    validate_slot "$slot"; stop_managed_backend "$slot"
    ;;
  restart)
    validate_slot "$slot"; ensure_prerequisites; ensure_vnc_display "$slot"; stop_managed_backend "$slot"; start_backend "$slot"
    ;;
  stop-vnc)
    validate_slot "$slot"; stop_vnc "$slot"
    ;;
  *) usage; exit 2 ;;
esac
CONTROL_EOF

python3 - "$CONTROL_SCRIPT" "$PROJECT_DIR" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
path.write_text(path.read_text(encoding="utf-8").replace("__PROJECT_DIR__", sys.argv[2]), encoding="utf-8")
PY
chmod 700 "$CONTROL_SCRIPT"

if command -v xdg-user-dir >/dev/null 2>&1; then
  DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
else
  DESKTOP_DIR="$HOME/Desktop"
fi
[[ -n "${DESKTOP_DIR:-}" ]] || DESKTOP_DIR="$HOME/Desktop"
mkdir -p "$DESKTOP_DIR"

for slot in 1 2 3; do
  desktop_file="$DESKTOP_DIR/Scraper $slot VNC.desktop"
  cat > "$desktop_file" <<DESKTOP_EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Scraper $slot VNC
Comment=Run OTA Scraper $slot inside isolated TigerVNC display :$slot
Exec=$CONTROL_SCRIPT open $slot
Icon=utilities-terminal
Terminal=true
Categories=Development;Network;
StartupNotify=true
DESKTOP_EOF
  chmod +x "$desktop_file"
  command -v gio >/dev/null 2>&1 && gio set "$desktop_file" metadata::trusted true >/dev/null 2>&1 || true
done

cat <<EOF

TigerVNC isolation launcher v2 installed.

No running scraper was stopped by this installer.

To repair Scraper 1 after stopping any active collection from its UI, run:
  "$CONTROL_SCRIPT" repair 1

To inspect the current port-8501 process first:
  "$CONTROL_SCRIPT" diagnose 1

Future launches:
  "$CONTROL_SCRIPT" open 1
EOF
