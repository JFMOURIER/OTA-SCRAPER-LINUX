#!/usr/bin/env bash
# Restore the original Scraper 1 instance and isolate its visible browser in a fresh TigerVNC X display.
set -Eeuo pipefail

PROJECT="${OTA_PROJECT_DIR:-/home/jf/Projects/OTA-SCRAPER-LINUX}"
PORT=8501
VNC_NUM=11
VNC_DISPLAY=":${VNC_NUM}"
VNC_PORT=$((5900 + VNC_NUM))
DATA_DIR="$PROJECT/data/instances/near_30_days"
INSTANCE_ID="near_30_days"
PERMANENT="$PROJECT/repair_scraper1_vnc_v3.sh"
PIDFILE="$DATA_DIR/status/streamlit_vnc11.pid"
PGIDFILE="$DATA_DIR/status/streamlit_vnc11.pgid"
LOGFILE="$DATA_DIR/logs/vnc11_launcher.log"
RUNTIME_DIR="$DATA_DIR/xdg_runtime_vnc11"
XSTARTUP="$HOME/.vnc/xstartup-ota-scrapers"
ACTION="${1:-repair}"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

server_cmd() {
  if command -v tigervncserver >/dev/null 2>&1; then command -v tigervncserver
  elif command -v vncserver >/dev/null 2>&1; then command -v vncserver
  else return 1
  fi
}

viewer_cmd() {
  if command -v vncviewer >/dev/null 2>&1; then command -v vncviewer
  elif command -v xtigervncviewer >/dev/null 2>&1; then command -v xtigervncviewer
  else return 1
  fi
}

listener_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u
  else
    fuser -n tcp "$PORT" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u || true
  fi
}

port_open() {
  python3 - "$PORT" <<'PY' >/dev/null 2>&1
import socket, sys
with socket.socket() as s:
    s.settimeout(.4)
    raise SystemExit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

proc_env() {
  local pid="$1" key="$2"
  [[ -r "/proc/$pid/environ" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^${key}=//p" | head -n1
}

active_job_file() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  python3 - "$file" <<'PY'
from datetime import datetime
import json, sys
try:
    d=json.load(open(sys.argv[1], encoding='utf-8'))
except Exception:
    raise SystemExit(1)
status=str(d.get('status') or '').lower()
if status not in {'starting','running'}:
    raise SystemExit(1)
stamp=d.get('last_updated_at') or d.get('timestamp')
if not stamp:
    raise SystemExit(0)
try:
    age=(datetime.now()-datetime.fromisoformat(str(stamp))).total_seconds()
except Exception:
    raise SystemExit(0)
raise SystemExit(0 if age < 900 else 1)
PY
}

status_file_for_pid() {
  local pid="$1" d
  d="$(proc_env "$pid" INSTANCE_DATA_DIR 2>/dev/null || true)"
  [[ -n "$d" ]] || d="$DATA_DIR"
  [[ "$d" = /* ]] || d="$PROJECT/$d"
  printf '%s/status/current_job_status.json\n' "$d"
}

stop_port_safely() {
  local pid cmd sf pgid i
  mapfile -t pids < <(listener_pids)
  ((${#pids[@]})) || return 0
  for pid in "${pids[@]}"; do
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    [[ "$cmd" == *streamlit* && "$cmd" == *app.py* ]] || die "Port $PORT is owned by an unexpected process (PID $pid): $cmd"
    sf="$(status_file_for_pid "$pid")"
    if active_job_file "$sf"; then
      cat >&2 <<EOF
ERROR: Scraper 1 still reports an active collection. Nothing was killed.
Press Stop in the scraper interface, wait for it to stop, and run the same one-command repair again.
Status file: $sf
EOF
      exit 12
    fi
  done
  for pid in "${pids[@]}"; do
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    say "Stopping the temporary/wrong Scraper 1 backend PID $pid ..."
    if [[ "$pgid" =~ ^[0-9]+$ && "$pgid" != "$$" ]]; then
      kill -TERM -- "-$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    else
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for i in $(seq 1 25); do port_open || return 0; sleep 1; done
  die "Port $PORT did not close. It was not force-killed."
}

make_xstartup() {
  mkdir -p "$HOME/.vnc"
  cat > "$XSTARTUP" <<'EOF'
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
EOF
  chmod 700 "$XSTARTUP"
}

vnc_ready() {
  DISPLAY="$VNC_DISPLAY" XAUTHORITY="$HOME/.Xauthority" xdpyinfo >/dev/null 2>&1
}

ensure_clean_vnc() {
  local server i owner
  server="$(server_cmd)" || die "TigerVNC server is missing."
  make_xstartup

  # Use :11 deliberately: it avoids the pre-existing :1 desktop/port conflict.
  if vnc_ready || lsof -nP -iTCP:"$VNC_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    "$server" -kill "$VNC_DISPLAY" >/dev/null 2>&1 || true
    sleep 2
  fi
  if lsof -nP -iTCP:"$VNC_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    owner="$(lsof -nP -iTCP:"$VNC_PORT" -sTCP:LISTEN 2>/dev/null || true)"
    die "TCP port $VNC_PORT is occupied after the VNC reset:\n$owner"
  fi

  say "Starting a clean standalone TigerVNC desktop named SCRAPER 1 on $VNC_DISPLAY ..."
  "$server" "$VNC_DISPLAY" -localhost yes -geometry 1600x900 -depth 24 \
    -desktop "SCRAPER 1" -xstartup "$XSTARTUP"
  for i in $(seq 1 30); do vnc_ready && break; sleep 1; done
  vnc_ready || die "TigerVNC display $VNC_DISPLAY did not become ready."
  pgrep -af "(Xtigervnc|Xvnc).*${VNC_DISPLAY}([[:space:]]|$)" >/dev/null 2>&1 \
    || die "$VNC_DISPLAY is not owned by a standalone Xtigervnc/Xvnc process."
  say "Verified: $VNC_DISPLAY is a real standalone TigerVNC X desktop."
}

prepare_original_instance() {
  local profile isolated backup stamp
  mkdir -p "$DATA_DIR"/{logs,status,exports,screenshots,debug,checkpoints,partial} "$RUNTIME_DIR"
  chmod 700 "$RUNTIME_DIR"

  # Keep all established data/config/scheduler files in near_30_days, but never reuse its old GUI browser process/profile.
  profile="$DATA_DIR/browser_profile"
  isolated="$DATA_DIR/browser_profiles/vnc11"
  mkdir -p "$DATA_DIR/browser_profiles"
  if [[ -L "$profile" ]]; then
    if [[ "$(readlink -f "$profile" 2>/dev/null || true)" != "$(readlink -f "$isolated" 2>/dev/null || true)" ]]; then
      rm -f "$profile"
    fi
  elif [[ -e "$profile" ]]; then
    stamp="$(date +%Y%m%d_%H%M%S)"
    backup="$DATA_DIR/browser_profile.before_vnc_$stamp"
    mv "$profile" "$backup"
    say "Preserved the previous scraper browser profile at: $backup"
  fi
  mkdir -p "$isolated"
  [[ -L "$profile" ]] || ln -s "browser_profiles/vnc11" "$profile"
  touch "$isolated/.scraper1_vnc11_profile"
}

start_backend() {
  local streamlit pid pgid i actual_display actual_data actual_id
  streamlit="$PROJECT/.venv/bin/streamlit"
  [[ -x "$streamlit" ]] || die "Streamlit was not found at $streamlit"
  cd "$PROJECT"
  : > "$LOGFILE"
  setsid env \
    DISPLAY="$VNC_DISPLAY" \
    SCRAPER_EXPECTED_DISPLAY="$VNC_DISPLAY" \
    XAUTHORITY="$HOME/.Xauthority" \
    XDG_RUNTIME_DIR="$RUNTIME_DIR" \
    WAYLAND_DISPLAY= \
    XDG_SESSION_TYPE=x11 \
    GDK_BACKEND=x11 \
    QT_QPA_PLATFORM=xcb \
    DBUS_SESSION_BUS_ADDRESS= \
    INSTANCE_ID="$INSTANCE_ID" \
    INSTANCE_NAME="Scraper 1" \
    INSTANCE_PORT="$PORT" \
    INSTANCE_DATA_DIR="$DATA_DIR" \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PYTHONUNBUFFERED=1 \
    "$streamlit" run app.py --server.port "$PORT" --server.address 127.0.0.1 \
      --server.headless true --browser.gatherUsageStats false \
      >>"$LOGFILE" 2>&1 < /dev/null &
  pid=$!
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  echo "$pid" > "$PIDFILE"
  [[ "$pgid" =~ ^[0-9]+$ ]] && echo "$pgid" > "$PGIDFILE"

  for i in $(seq 1 45); do
    kill -0 "$pid" 2>/dev/null || { tail -n 80 "$LOGFILE" >&2 || true; die "Scraper 1 exited during startup."; }
    port_open && break
    sleep 1
  done
  port_open || { tail -n 80 "$LOGFILE" >&2 || true; die "Scraper 1 did not open port $PORT."; }

  mapfile -t pids < <(listener_pids)
  ((${#pids[@]})) || die "No listener PID was found on port $PORT."
  actual_display="$(proc_env "${pids[0]}" DISPLAY 2>/dev/null || true)"
  actual_data="$(proc_env "${pids[0]}" INSTANCE_DATA_DIR 2>/dev/null || true)"
  actual_id="$(proc_env "${pids[0]}" INSTANCE_ID 2>/dev/null || true)"
  [[ "$actual_display" == "$VNC_DISPLAY" ]] || die "Backend DISPLAY is $actual_display, expected $VNC_DISPLAY."
  [[ "$actual_data" == "$DATA_DIR" ]] || die "Backend data folder is $actual_data, expected $DATA_DIR."
  [[ "$actual_id" == "$INSTANCE_ID" ]] || die "Backend instance ID is $actual_id, expected $INSTANCE_ID."

  say "Verified backend: DISPLAY=$actual_display"
  say "Verified original instance: INSTANCE_ID=$actual_id"
  say "Verified original data: $actual_data"
}

open_viewer_and_browser() {
  local viewer browser profile url
  viewer="$(viewer_cmd)" || die "TigerVNC viewer is missing."
  nohup "$viewer" "localhost:${VNC_NUM}" >/dev/null 2>&1 &
  say "Opened the SCRAPER 1 TigerVNC viewer (internally display $VNC_DISPLAY)."

  profile="$DATA_DIR/control_browser_profile_vnc11"
  url="http://127.0.0.1:$PORT"
  mkdir -p "$profile"
  browser=""
  for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$candidate" >/dev/null 2>&1; then browser="$(command -v "$candidate")"; break; fi
  done
  if [[ -n "$browser" ]]; then
    nohup env DISPLAY="$VNC_DISPLAY" XAUTHORITY="$HOME/.Xauthority" XDG_RUNTIME_DIR="$RUNTIME_DIR" \
      WAYLAND_DISPLAY= XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 DBUS_SESSION_BUS_ADDRESS= \
      "$browser" --user-data-dir="$profile" --new-window "$url" --no-first-run \
      --no-default-browser-check --ozone-platform=x11 >/dev/null 2>&1 &
    say "Opened the control page inside the VNC desktop."
  else
    say "Open $url in a browser inside the VNC desktop."
  fi
}

write_permanent_files() {
  if [[ "$(readlink -f "$0")" != "$(readlink -f "$PERMANENT" 2>/dev/null || true)" ]]; then
    cp "$0" "$PERMANENT"
    chmod 700 "$PERMANENT"
  fi
  local desktop_dir desktop_file
  desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
  mkdir -p "$desktop_dir"
  desktop_file="$desktop_dir/Scraper 1 VNC.desktop"
  cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Scraper 1 VNC
Comment=Open the original near_30_days Scraper 1 in isolated TigerVNC display :11
Exec=$PERMANENT open
Icon=utilities-terminal
Terminal=true
Categories=Development;Network;
StartupNotify=true
EOF
  chmod +x "$desktop_file"
  command -v gio >/dev/null 2>&1 && gio set "$desktop_file" metadata::trusted true >/dev/null 2>&1 || true
}

show_status() {
  say "--- Scraper 1 VNC status ---"
  say "Expected VNC: $VNC_DISPLAY (viewer localhost:${VNC_NUM})"
  say "Expected data: $DATA_DIR"
  if vnc_ready; then say "VNC display: ready"; else say "VNC display: not ready"; fi
  mapfile -t pids < <(listener_pids)
  if ((${#pids[@]})); then
    for pid in "${pids[@]}"; do
      say "Port $PORT PID $pid DISPLAY=$(proc_env "$pid" DISPLAY 2>/dev/null || echo unset) INSTANCE_ID=$(proc_env "$pid" INSTANCE_ID 2>/dev/null || echo unset) DATA=$(proc_env "$pid" INSTANCE_DATA_DIR 2>/dev/null || echo unset)"
    done
  else
    say "Port $PORT: no listener"
  fi
  say "Visible Chromium/Chrome processes and their DISPLAY values:"
  for pid in $(pgrep -f '(chrome|chromium)' 2>/dev/null || true); do
    local_cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    if [[ "$local_cmd" == *"$PROJECT"* || "$local_cmd" == *"playwright"* ]]; then
      say "  PID $pid DISPLAY=$(proc_env "$pid" DISPLAY 2>/dev/null || echo unset) CMD=${local_cmd:0:180}"
    fi
  done
}

stop_managed() {
  local pid pgid i
  [[ -s "$PIDFILE" ]] || { say "No managed Scraper 1 PID file."; return 0; }
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    if active_job_file "$DATA_DIR/status/current_job_status.json"; then
      die "An active collection is reported. Stop it from the interface first."
    fi
    pgid="$(cat "$PGIDFILE" 2>/dev/null || true)"
    if [[ "$pgid" =~ ^[0-9]+$ ]]; then kill -TERM -- "-$pgid" 2>/dev/null || true; else kill -TERM "$pid" 2>/dev/null || true; fi
    for i in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  fi
  rm -f "$PIDFILE" "$PGIDFILE"
  say "Managed Scraper 1 stopped."
}

main_repair() {
  [[ -d "$PROJECT" && -f "$PROJECT/app.py" ]] || die "Project not found at $PROJECT"
  [[ -f "$HOME/.vnc/passwd" ]] || die "TigerVNC password missing. Run vncpasswd once."
  command -v xdpyinfo >/dev/null 2>&1 || die "xdpyinfo is missing (install x11-utils)."
  command -v lsof >/dev/null 2>&1 || die "lsof is missing."
  server_cmd >/dev/null || die "TigerVNC standalone server is missing."
  viewer_cmd >/dev/null || die "TigerVNC viewer is missing."

  stop_port_safely
  ensure_clean_vnc
  prepare_original_instance
  start_backend
  write_permanent_files
  open_viewer_and_browser
  say ""
  say "SUCCESS: Scraper 1 now uses the ORIGINAL near_30_days instance and a fresh standalone VNC display $VNC_DISPLAY."
  say "The new blank scraper_1 folder was not deleted."
  say "Start only a very small test scrape first."
  say "After the browser opens, run this status command only if it still appears outside:"
  say "  $PERMANENT status"
}

case "$ACTION" in
  repair|install) main_repair ;;
  open)
    if port_open && vnc_ready; then write_permanent_files; open_viewer_and_browser
    else main_repair
    fi
    ;;
  status) show_status ;;
  stop) stop_managed ;;
  *) die "Usage: $0 [repair|open|status|stop]" ;;
esac
