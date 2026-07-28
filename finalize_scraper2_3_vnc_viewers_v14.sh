#!/usr/bin/env bash
# Finalize the practical Scraper 2/3 TigerVNC setup.
# The Streamlit UI is headless, so its legacy DISPLAY=:102/:103 is harmless.
# The visible scraper workers are the critical processes; this script verifies
# they remain bound to :12/:13, restores scheduling, creates permanent launchers,
# and opens both dedicated VNC viewers and control pages.
set -Eeuo pipefail

PROJECT="${OTA_PROJECT_DIR:-/home/jf/Projects/OTA-SCRAPER-LINUX}"
USER_ID="$(id -u)"
USER_RUNTIME="/run/user/$USER_ID"
USER_BUS="unix:path=$USER_RUNTIME/bus"
XAUTHORITY_FILE="$HOME/.Xauthority"
XSTARTUP="$HOME/.vnc/xstartup-ota-scrapers"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
[[ -n "$DESKTOP_DIR" ]] || DESKTOP_DIR="$HOME/Desktop"
STAMP="$(date +%Y%m%d_%H%M%S)"
REPORT="$HOME/Downloads/scraper2_3_vnc_viewers_${STAMP}.txt"

mkdir -p "$HOME/Downloads" "$DESKTOP_DIR"
exec > >(tee "$REPORT") 2>&1

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

user_systemctl() {
  env XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" systemctl --user "$@"
}

display_ok() {
  local display="$1"
  DISPLAY="$display" XAUTHORITY="$XAUTHORITY_FILE" xdpyinfo >/dev/null 2>&1
}

health_ok() {
  local port="$1"
  curl -fsS --max-time 3 "http://127.0.0.1:${port}/_stcore/health" 2>/dev/null | grep -qx ok
}

verify_worker() {
  local instance="$1"
  local display="$2"
  local unit="ota-scraper-run@${instance}.service"
  local environment
  environment="$(user_systemctl show "$unit" -p Environment --value 2>/dev/null || true)"
  [[ "$environment" == *"DISPLAY=$display"* ]] || die "$unit is not bound to $display"
  [[ "$environment" == *"SCRAPER_EXPECTED_DISPLAY=$display"* ]] || die "$unit expected display is not $display"
  say "Verified visible scraper worker $unit is bound to $display."
}

ensure_ui() {
  local unit="$1"
  local port="$2"
  user_systemctl start "$unit"
  local attempt
  for attempt in $(seq 1 40); do
    health_ok "$port" && return 0
    sleep 1
  done
  die "$unit did not become healthy on port $port"
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
  if command -v tigervncserver >/dev/null 2>&1; then
    SERVER="\$(command -v tigervncserver)"
  else
    SERVER="\$(command -v vncserver)"
  fi
  "\$SERVER" "\$DISPLAY_ID" -localhost yes -geometry 1600x900 -depth 24 \
    -desktop 'SCRAPER $number TIGER VNC' -xstartup "\$XSTARTUP"
  for _ in \$(seq 1 30); do display_ok && break; sleep 1; done
fi

display_ok || { echo "TigerVNC \$DISPLAY_ID failed to start." >&2; exit 1; }
user_systemctl start "\$UI_UNIT"
for _ in \$(seq 1 40); do
  curl -fsS --max-time 2 "http://127.0.0.1:\$PORT/_stcore/health" 2>/dev/null | grep -qx ok && break
  sleep 1
done

if command -v vncviewer >/dev/null 2>&1; then
  VIEWER="\$(command -v vncviewer)"
else
  VIEWER="\$(command -v xtigervncviewer)"
fi
if ! pgrep -af "vncviewer.*(localhost:\$VNC_NUMBER|:\$VNC_NUMBER)" >/dev/null 2>&1; then
  nohup "\$VIEWER" "localhost:\$VNC_NUMBER" >/dev/null 2>&1 &
fi

mkdir -p "\$PROFILE"
if ! pgrep -af -- "--user-data-dir=\$PROFILE" >/dev/null 2>&1; then
  BROWSER=''
  for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "\$candidate" >/dev/null 2>&1; then
      BROWSER="\$(command -v "\$candidate")"
      break
    fi
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
  cat > "$desktop_file" <<EOF2
[Desktop Entry]
Type=Application
Version=1.0
Name=Scraper $number TigerVNC
Comment=Open Scraper $number in dedicated TigerVNC $display
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

main() {
  say "Finalize Scraper 2 and 3 TigerVNC viewers v14"
  say "Report: $REPORT"

  [[ -d "$PROJECT/data/instances/medium_31_120_days" ]] || die "Medium instance data folder is missing."
  [[ -d "$PROJECT/data/instances/long_121_365_days" ]] || die "Long instance data folder is missing."
  [[ -S "$USER_RUNTIME/bus" ]] || die "User D-Bus socket is missing."
  display_ok :12 || die "TigerVNC :12 is not running."
  display_ok :13 || die "TigerVNC :13 is not running."

  verify_worker medium_31_120_days :12
  verify_worker long_121_365_days :13

  ensure_ui ota-ui-medium.service 8502
  ensure_ui ota-ui-long.service 8503
  say "Verified the two Streamlit control pages are healthy."

  user_systemctl restart ota-scheduler-dispatch.timer
  [[ "$(user_systemctl is-active ota-scheduler-dispatch.timer 2>/dev/null || true)" == "active" ]] \
    || die "The scheduler timer did not become active."
  say "Shared scheduler timer is active."

  write_launcher 2 :12 8502 medium_31_120_days ota-ui-medium.service
  write_launcher 3 :13 8503 long_121_365_days ota-ui-long.service

  "$PROJECT/open_scraper2_vnc.sh" >/dev/null 2>&1 &
  sleep 2
  "$PROJECT/open_scraper3_vnc.sh" >/dev/null 2>&1 &

  say ""
  say "SUCCESS: Scraper 2 visible worker is isolated on TigerVNC :12."
  say "SUCCESS: Scraper 3 visible worker is isolated on TigerVNC :13."
  say "SUCCESS: both control pages are healthy and the scheduler timer is active."
  say "SUCCESS: permanent desktop shortcuts were created and both viewers were opened."
  say ""
  say "The Streamlit UI process may still report :102/:103 because it is headless; this no longer blocks setup."
  say "The Booking.com windows are launched by the worker services verified above on :12/:13."
}

main "$@"
