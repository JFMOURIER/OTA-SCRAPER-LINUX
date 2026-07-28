#!/usr/bin/env bash
# Complete dedicated TigerVNC setup for Scrapers 2 and 3 after the v11 installer.
# Replaces legacy UI launch commands that still force DISPLAY=:102/:103,
# preserves all data/configuration, restarts the scheduler timer, creates
# permanent desktop launchers, and opens both viewers.
set -Eeuo pipefail

PROJECT="${OTA_PROJECT_DIR:-/home/jf/Projects/OTA-SCRAPER-LINUX}"
VENV="$PROJECT/.venv"
USER_ID="$(id -u)"
USER_RUNTIME="/run/user/$USER_ID"
USER_BUS="unix:path=$USER_RUNTIME/bus"
XAUTHORITY_FILE="$HOME/.Xauthority"
STAMP="$(date +%Y%m%d_%H%M%S)"
REPORT="$HOME/Downloads/scraper2_3_vnc_completion_${STAMP}.txt"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
[[ -n "$DESKTOP_DIR" ]] || DESKTOP_DIR="$HOME/Desktop"
XSTARTUP="$HOME/.vnc/xstartup-ota-scrapers"

mkdir -p "$HOME/Downloads" "$DESKTOP_DIR"
exec > >(tee "$REPORT") 2>&1

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

user_systemctl() {
  env XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" systemctl --user "$@"
}

proc_env() {
  local pid="$1"
  local key="$2"
  [[ -r "/proc/$pid/environ" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^${key}=//p" | head -n 1
}

health_ok() {
  local port="$1"
  curl -fsS --max-time 2 "http://127.0.0.1:${port}/_stcore/health" 2>/dev/null | grep -qx ok
}

display_ok() {
  local display="$1"
  DISPLAY="$display" XAUTHORITY="$XAUTHORITY_FILE" xdpyinfo >/dev/null 2>&1
}

worker_active() {
  local instance="$1"
  local unit="ota-scraper-run@${instance}.service"
  [[ "$(user_systemctl is-active "$unit" 2>/dev/null || true)" == "active" ]] && return 0
  pgrep -af "ota_scheduled_run.py.*${instance}" >/dev/null 2>&1 && return 0
  return 1
}

verify_prerequisites() {
  [[ -d "$PROJECT" && -f "$PROJECT/app.py" ]] || die "Project not found at $PROJECT"
  [[ -x "$VENV/bin/streamlit" ]] || die "Streamlit executable missing: $VENV/bin/streamlit"
  [[ -S "$USER_RUNTIME/bus" ]] || die "User D-Bus socket missing: $USER_RUNTIME/bus"
  command -v xdpyinfo >/dev/null 2>&1 || die "xdpyinfo is missing."
  command -v curl >/dev/null 2>&1 || die "curl is missing."
  user_systemctl show-environment >/dev/null 2>&1 || die "User systemd is not reachable."
  display_ok :12 || die "TigerVNC :12 is not running."
  display_ok :13 || die "TigerVNC :13 is not running."
  [[ -d "$PROJECT/data/instances/medium_31_120_days" ]] || die "Medium instance data folder is missing."
  [[ -d "$PROJECT/data/instances/long_121_365_days" ]] || die "Long instance data folder is missing."
  user_systemctl cat ota-ui-medium.service >/dev/null 2>&1 || die "Missing ota-ui-medium.service"
  user_systemctl cat ota-ui-long.service >/dev/null 2>&1 || die "Missing ota-ui-long.service"
  user_systemctl cat ota-scraper-run@medium_31_120_days.service >/dev/null 2>&1 || die "Missing medium worker service"
  user_systemctl cat ota-scraper-run@long_121_365_days.service >/dev/null 2>&1 || die "Missing long worker service"
  say "Verified TigerVNC :12 and :13, both original data folders, and user-systemd."
}

write_direct_ui_dropin() {
  local unit="$1"
  local instance="$2"
  local name="$3"
  local port="$4"
  local display="$5"
  local data_dir="$PROJECT/data/instances/$instance"
  local directory="$HOME/.config/systemd/user/${unit}.d"
  local file="$directory/zzzzzz-direct-vnc-ui.conf"
  mkdir -p "$directory"
  [[ -f "$file" ]] && cp -a "$file" "$file.backup.$STAMP"

  cat > "$file" <<EOF2
[Service]
WorkingDirectory=$PROJECT
ExecStart=
ExecStart=$VENV/bin/streamlit run app.py --server.port $port --server.address 127.0.0.1 --server.headless true --browser.gatherUsageStats false
Environment="DISPLAY=$display"
Environment="SCRAPER_EXPECTED_DISPLAY=$display"
Environment="INSTANCE_ID=$instance"
Environment="INSTANCE_NAME=$name"
Environment="INSTANCE_PORT=$port"
Environment="INSTANCE_DATA_DIR=$data_dir"
Environment="XAUTHORITY=$XAUTHORITY_FILE"
Environment="XDG_RUNTIME_DIR=$USER_RUNTIME"
Environment="DBUS_SESSION_BUS_ADDRESS=$USER_BUS"
Environment="WAYLAND_DISPLAY="
Environment="XDG_SESSION_TYPE=x11"
Environment="GDK_BACKEND=x11"
Environment="QT_QPA_PLATFORM=xcb"
Environment="OZONE_PLATFORM=x11"
Environment="NO_AT_BRIDGE=1"
EOF2
  say "Installed direct VNC UI launch for $unit on $display."
}

verify_worker_binding() {
  local instance="$1"
  local display="$2"
  local unit="ota-scraper-run@${instance}.service"
  local environment
  environment="$(user_systemctl show "$unit" -p Environment --value 2>/dev/null || true)"
  [[ "$environment" == *"DISPLAY=$display"* ]] || die "$unit is not bound to $display"
  [[ "$environment" == *"SCRAPER_EXPECTED_DISPLAY=$display"* ]] || die "$unit expected display is not $display"
  say "Verified $unit remains bound to $display."
}

restart_and_verify_ui() {
  local unit="$1"
  local instance="$2"
  local port="$3"
  local display="$4"
  local expected_data="$PROJECT/data/instances/$instance"
  local attempt pid actual_display actual_instance actual_data

  user_systemctl reset-failed "$unit" >/dev/null 2>&1 || true
  user_systemctl restart "$unit"
  for attempt in $(seq 1 60); do
    health_ok "$port" && break
    if [[ "$(user_systemctl is-failed "$unit" 2>/dev/null || true)" == "failed" ]]; then
      user_systemctl status "$unit" --no-pager >&2 || true
      env XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" \
        journalctl --user -u "$unit" -n 100 --no-pager >&2 || true
      die "$unit failed to start."
    fi
    sleep 1
  done
  health_ok "$port" || die "$unit did not become healthy on port $port."

  pid="$(user_systemctl show "$unit" -p MainPID --value | tr -d ' ')"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || die "Could not determine PID for $unit"
  actual_display="$(proc_env "$pid" DISPLAY 2>/dev/null || true)"
  actual_instance="$(proc_env "$pid" INSTANCE_ID 2>/dev/null || true)"
  actual_data="$(proc_env "$pid" INSTANCE_DATA_DIR 2>/dev/null || true)"
  [[ "$actual_display" == "$display" ]] || die "$unit DISPLAY is '$actual_display', expected '$display'."
  [[ "$actual_instance" == "$instance" ]] || die "$unit INSTANCE_ID is '$actual_instance', expected '$instance'."
  [[ "$actual_data" == "$expected_data" ]] || die "$unit data folder is '$actual_data', expected '$expected_data'."
  say "Verified $unit PID $pid on $display, port $port, original data=$actual_data."
}

write_launcher() {
  local number="$1"
  local display="$2"
  local port="$3"
  local instance="$4"
  local ui_unit="$5"
  local launcher="$PROJECT/open_scraper${number}_vnc.sh"
  local profile="$PROJECT/data/instances/$instance/control_browser_profile_vnc${display#:}"

  cat > "$launcher" <<EOF2
#!/usr/bin/env bash
set -Eeuo pipefail
DISPLAY_ID='$display'
VNC_NUMBER='${display#:}'
PORT='$port'
UI_UNIT='$ui_unit'
PROFILE='$profile'
USER_RUNTIME='$USER_RUNTIME'
USER_BUS='$USER_BUS'
XAUTHORITY_FILE='$XAUTHORITY_FILE'
XSTARTUP='$XSTARTUP'

user_systemctl() {
  env XDG_RUNTIME_DIR="\$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="\$USER_BUS" systemctl --user "\$@"
}

display_ok() {
  DISPLAY="\$DISPLAY_ID" XAUTHORITY="\$XAUTHORITY_FILE" xdpyinfo >/dev/null 2>&1
}

if ! display_ok; then
  if command -v tigervncserver >/dev/null 2>&1; then SERVER="\$(command -v tigervncserver)"; else SERVER="\$(command -v vncserver)"; fi
  "\$SERVER" "\$DISPLAY_ID" -localhost yes -geometry 1600x900 -depth 24 -desktop 'SCRAPER $number TIGER VNC' -xstartup "\$XSTARTUP"
  for _ in \$(seq 1 30); do display_ok && break; sleep 1; done
fi

display_ok || { echo "TigerVNC \$DISPLAY_ID failed to start." >&2; exit 1; }
user_systemctl start "\$UI_UNIT"
for _ in \$(seq 1 40); do
  curl -fsS --max-time 2 "http://127.0.0.1:\$PORT/_stcore/health" 2>/dev/null | grep -qx ok && break
  sleep 1
done

if command -v vncviewer >/dev/null 2>&1; then VIEWER="\$(command -v vncviewer)"; else VIEWER="\$(command -v xtigervncviewer)"; fi
if ! pgrep -af "vncviewer.*(localhost:\$VNC_NUMBER|:\$VNC_NUMBER)" >/dev/null 2>&1; then
  nohup "\$VIEWER" "localhost:\$VNC_NUMBER" >/dev/null 2>&1 &
fi

mkdir -p "\$PROFILE"
if ! pgrep -af -- "--user-data-dir=\$PROFILE" >/dev/null 2>&1; then
  BROWSER=''
  for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "\$candidate" >/dev/null 2>&1; then BROWSER="\$(command -v "\$candidate")"; break; fi
  done
  if [[ -n "\$BROWSER" ]]; then
    nohup env DISPLAY="\$DISPLAY_ID" SCRAPER_EXPECTED_DISPLAY="\$DISPLAY_ID" \
      XAUTHORITY="\$XAUTHORITY_FILE" XDG_RUNTIME_DIR="\$USER_RUNTIME" \
      DBUS_SESSION_BUS_ADDRESS="\$USER_BUS" WAYLAND_DISPLAY= XDG_SESSION_TYPE=x11 \
      GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb OZONE_PLATFORM=x11 \
      "\$BROWSER" --user-data-dir="\$PROFILE" --new-window "http://127.0.0.1:\$PORT" \
      --no-first-run --no-default-browser-check --ozone-platform=x11 >/dev/null 2>&1 &
  fi
fi
EOF2
  chmod 700 "$launcher"

  local desktop_file="$DESKTOP_DIR/Scraper $number TigerVNC.desktop"
  [[ -f "$desktop_file" ]] && cp -a "$desktop_file" "$desktop_file.backup.$STAMP"
  cat > "$desktop_file" <<EOF2
[Desktop Entry]
Type=Application
Version=1.0
Name=Scraper $number TigerVNC
Comment=Open Scraper $number in its dedicated TigerVNC desktop $display
Exec=$launcher
Icon=utilities-terminal
Terminal=false
Categories=Development;Network;
StartupNotify=true
EOF2
  chmod +x "$desktop_file"
  command -v gio >/dev/null 2>&1 && gio set "$desktop_file" metadata::trusted true >/dev/null 2>&1 || true
  say "Created desktop shortcut: $desktop_file"
}

open_both() {
  "$PROJECT/open_scraper2_vnc.sh" >/dev/null 2>&1 &
  sleep 2
  "$PROJECT/open_scraper3_vnc.sh" >/dev/null 2>&1 &
  say "Opened the Scraper 2 and Scraper 3 TigerVNC viewers."
}

main() {
  say "Complete Scraper 2 and 3 dedicated TigerVNC setup v12"
  say "Report: $REPORT"
  verify_prerequisites

  if worker_active medium_31_120_days; then
    die "Scraper 2 is actively scraping. Stop it first; nothing was changed."
  fi
  if worker_active long_121_365_days; then
    die "Scraper 3 is actively scraping. Stop it first; nothing was changed."
  fi

  user_systemctl stop ota-scheduler-dispatch.timer >/dev/null 2>&1 || true
  write_direct_ui_dropin ota-ui-medium.service medium_31_120_days "Scraper 2" 8502 :12
  write_direct_ui_dropin ota-ui-long.service long_121_365_days "Scraper 3" 8503 :13
  user_systemctl daemon-reload

  verify_worker_binding medium_31_120_days :12
  verify_worker_binding long_121_365_days :13
  restart_and_verify_ui ota-ui-medium.service medium_31_120_days 8502 :12
  restart_and_verify_ui ota-ui-long.service long_121_365_days 8503 :13

  user_systemctl restart ota-scheduler-dispatch.timer
  [[ "$(user_systemctl is-active ota-scheduler-dispatch.timer 2>/dev/null || true)" == "active" ]] \
    || die "The shared scheduler timer did not restart."
  say "Shared scheduler timer is active again."

  write_launcher 2 :12 8502 medium_31_120_days ota-ui-medium.service
  write_launcher 3 :13 8503 long_121_365_days ota-ui-long.service
  open_both

  say ""
  say "SUCCESS: Scraper 2 UI and worker are isolated on TigerVNC :12 and port 8502."
  say "SUCCESS: Scraper 3 UI and worker are isolated on TigerVNC :13 and port 8503."
  say "SUCCESS: the shared scheduler timer is active again."
  say "SUCCESS: permanent desktop shortcuts were created."
  say "SUCCESS: all original data, schedules, exports, checkpoints and profiles were preserved."
}

main "$@"
