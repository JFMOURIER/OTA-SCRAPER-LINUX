#!/usr/bin/env bash
# Read-only runtime diagnosis for Scraper 1 on the restored near_30_days instance.
# It creates a text report and a temporary Playwright smoke-test screenshot only.
set -u
set +e

PROJECT="${OTA_PROJECT_DIR:-/home/jf/Projects/OTA-SCRAPER-LINUX}"
DATA_DIR="$PROJECT/data/instances/near_30_days"
DISPLAY_ID=":11"
PORT=8501
VNC_PORT=5911
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$HOME/Downloads/scraper1_run_diagnostic_${STAMP}.txt"
SMOKE_DIR="$DATA_DIR/debug/vnc11_smoke_${STAMP}"

mkdir -p "$HOME/Downloads" "$SMOKE_DIR"
exec > >(tee "$OUT") 2>&1

section() {
  printf '\n\n============================================================\n%s\n============================================================\n' "$1"
}

proc_env() {
  local pid="$1" key="$2"
  [[ -r "/proc/$pid/environ" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^${key}=//p" | head -n1
}

listener_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u
  elif command -v fuser >/dev/null 2>&1; then
    fuser -n tcp "$PORT" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u
  else
    ss -ltnp "sport = :$PORT" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u
  fi
}

section "BASIC INFORMATION"
date --iso-8601=seconds 2>/dev/null || date
printf 'Project: %s\n' "$PROJECT"
printf 'Original data instance: %s\n' "$DATA_DIR"
printf 'Expected VNC display: %s\n' "$DISPLAY_ID"
printf 'Expected Streamlit port: %s\n' "$PORT"
printf 'User: %s  Home: %s\n' "$(id -un)" "$HOME"
uname -a
printf '\nDisk and memory:\n'
df -h "$PROJECT" "$HOME/Downloads" 2>/dev/null
free -h 2>/dev/null

section "GIT AND LOCAL FILE STATE"
if [[ -d "$PROJECT/.git" ]]; then
  git -C "$PROJECT" status --short --branch 2>&1
  printf '\nRecent local commits:\n'
  git -C "$PROJECT" log --oneline -8 2>&1
else
  printf 'No .git directory at %s\n' "$PROJECT"
fi
printf '\nTop-level files with modification times:\n'
find "$PROJECT" -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM:%TS %10s %p\n' 2>/dev/null | sort -r | head -80

section "PORT 8501 BACKEND AND ENVIRONMENT"
mapfile -t PORT_PIDS < <(listener_pids)
if ((${#PORT_PIDS[@]} == 0)); then
  printf 'NO PROCESS IS LISTENING ON PORT %s\n' "$PORT"
else
  for pid in "${PORT_PIDS[@]}"; do
    printf '\nListener PID: %s\n' "$pid"
    printf 'Command: '
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null; printf '\n'
    ps -o pid,ppid,pgid,sid,stat,etime,%cpu,%mem,cmd -p "$pid" 2>&1
    for key in DISPLAY SCRAPER_EXPECTED_DISPLAY INSTANCE_ID INSTANCE_NAME INSTANCE_PORT INSTANCE_DATA_DIR XDG_RUNTIME_DIR WAYLAND_DISPLAY XDG_SESSION_TYPE DBUS_SESSION_BUS_ADDRESS; do
      printf '%-28s %s\n' "$key:" "$(proc_env "$pid" "$key" 2>/dev/null || printf '<not set>')"
    done
    printf '\nChildren and descendants:\n'
    ps --forest -o pid,ppid,pgid,sid,stat,etime,%cpu,%mem,cmd --ppid "$pid" 2>&1
    if command -v pstree >/dev/null 2>&1; then pstree -ap "$pid" 2>&1; fi
  done
fi

section "STREAMLIT HTTP HEALTH"
if command -v curl >/dev/null 2>&1; then
  printf 'Health endpoint:\n'
  curl -m 5 -sS -i "http://127.0.0.1:${PORT}/_stcore/health" 2>&1
  printf '\nMain page first response lines:\n'
  curl -m 5 -sS -i "http://127.0.0.1:${PORT}/" 2>&1 | head -40
else
  printf 'curl is not installed.\n'
fi

section "STANDALONE TIGERVNC DISPLAY CHECK"
printf 'TCP listener on %s:\n' "$VNC_PORT"
if command -v lsof >/dev/null 2>&1; then lsof -nP -iTCP:"$VNC_PORT" -sTCP:LISTEN 2>&1; else ss -ltnp "sport = :$VNC_PORT" 2>&1; fi
printf '\nXvnc / Xtigervnc processes:\n'
pgrep -af 'Xtigervnc|Xvnc|x0vncserver|vncserver' 2>&1
printf '\nX display probe:\n'
DISPLAY="$DISPLAY_ID" XAUTHORITY="$HOME/.Xauthority" xdpyinfo 2>&1 | head -45

section "SCRAPER, SCHEDULER, STREAMLIT AND BROWSER PROCESSES"
ps -eo user,pid,ppid,pgid,sid,stat,lstart,etime,%cpu,%mem,cmd --sort=pid 2>/dev/null \
  | grep -Ei 'streamlit|app\.py|scheduler|scraper|playwright|chrome|chromium|Xtigervnc|Xvnc' \
  | grep -v -E 'grep -E|diagnose_scraper1_run_v4' \
  | tail -250

printf '\nRelevant process DISPLAY values:\n'
for pid in $(pgrep -f 'streamlit|app\.py|scheduler|scraper|playwright|chrome|chromium' 2>/dev/null | sort -u); do
  cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  case "$cmd" in
    *"$PROJECT"*|*playwright*|*streamlit*|*chrome*|*chromium*)
      printf 'PID=%-8s PPID=%-8s PGID=%-8s DISPLAY=%-8s CMD=%s\n' \
        "$pid" \
        "$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')" \
        "$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')" \
        "$(proc_env "$pid" DISPLAY 2>/dev/null || printf '<unset>')" \
        "${cmd:0:260}"
      ;;
  esac
done

section "ORIGINAL INSTANCE DIRECTORY"
if [[ ! -d "$DATA_DIR" ]]; then
  printf 'MISSING DATA DIRECTORY: %s\n' "$DATA_DIR"
else
  du -sh "$DATA_DIR" 2>&1
  find "$DATA_DIR" -maxdepth 2 -type f -printf '%TY-%Tm-%Td %TH:%TM:%TS %10s %p\n' 2>/dev/null | sort -r | head -180
  printf '\nBrowser profile path:\n'
  ls -ld "$DATA_DIR/browser_profile" "$DATA_DIR/browser_profiles" "$DATA_DIR/browser_profiles/vnc11" 2>&1
  readlink -f "$DATA_DIR/browser_profile" 2>&1
fi

section "STATUS, HEARTBEAT, LOCK AND SCHEDULER STATE FILES"
if [[ -d "$DATA_DIR" ]]; then
  find "$DATA_DIR" -maxdepth 4 -type f \
    \( -iname '*status*' -o -iname '*heartbeat*' -o -iname '*lock*' -o -iname '*pid*' -o -iname '*schedul*' -o -iname '*setting*' -o -iname '*config*' \) \
    -printf '%TY-%Tm-%Td %TH:%TM:%TS %10s %p\n' 2>/dev/null | sort -r | head -250

  printf '\nReadable JSON/TOML/YAML scheduler and runtime files (maximum 80 KB each):\n'
  while IFS= read -r file; do
    size="$(stat -c %s "$file" 2>/dev/null || echo 999999999)"
    if [[ "$size" -le 81920 ]]; then
      printf '\n----- %s (mtime %s, %s bytes) -----\n' "$file" "$(stat -c %y "$file" 2>/dev/null)" "$size"
      case "$file" in
        *.json)
          "$PROJECT/.venv/bin/python" - "$file" <<'PY' 2>/dev/null || cat "$file"
import json, sys
p=sys.argv[1]
with open(p, encoding='utf-8', errors='replace') as f:
    value=json.load(f)
print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
PY
          ;;
        *) sed -n '1,300p' "$file" ;;
      esac
    fi
  done < <(find "$DATA_DIR" -maxdepth 4 -type f \
    \( -iname '*.json' -o -iname '*.toml' -o -iname '*.yaml' -o -iname '*.yml' \) \
    \( -iname '*status*' -o -iname '*heartbeat*' -o -iname '*schedul*' -o -iname '*setting*' -o -iname '*config*' -o -iname '*error*' \) 2>/dev/null | sort)
fi

section "LATEST LOGS AND ERRORS"
if [[ -d "$DATA_DIR/logs" ]]; then
  printf 'Log files newest first:\n'
  find "$DATA_DIR/logs" -maxdepth 2 -type f -printf '%T@ %TY-%Tm-%Td %TH:%TM:%TS %10s %p\n' 2>/dev/null \
    | sort -nr | head -30 | cut -d' ' -f2-

  mapfile -t LATEST_LOGS < <(find "$DATA_DIR/logs" -maxdepth 2 -type f -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -8 | cut -d' ' -f2-)
  for file in "${LATEST_LOGS[@]}"; do
    printf '\n----- LAST 220 LINES: %s -----\n' "$file"
    tail -n 220 "$file" 2>&1
  done
else
  printf 'Missing logs directory: %s/logs\n' "$DATA_DIR"
fi

printf '\nRecent error-like lines across original-instance logs:\n'
grep -RniE 'traceback|exception|error|failed|failure|timeout|browser.*closed|target.*closed|profile.*lock|singleton|display|x server|scheduler' \
  "$DATA_DIR/logs" "$DATA_DIR/status" "$DATA_DIR/debug" 2>/dev/null \
  | tail -350

section "SCHEDULER AND RUN-BUTTON CODE DISCOVERY"
printf 'Files with scheduler/run/service names:\n'
find "$PROJECT" -maxdepth 5 -type f \
  \( -iname '*schedul*' -o -iname '*runner*' -o -iname '*launcher*' -o -iname '*service*' -o -iname '*scraper*' \) \
  -not -path '*/.git/*' -not -path '*/.venv/*' -not -path '*/data/*' \
  -printf '%TY-%Tm-%Td %TH:%TM:%TS %10s %p\n' 2>/dev/null | sort -r | head -220

printf '\nRelevant code references (limited to 420 matches):\n'
grep -RniE 'scheduler|schedule_|runs per day|run now|run scraper|start.*job|start_background|subprocess|systemctl|st\.button|form_submit_button' \
  "$PROJECT" \
  --include='*.py' --include='*.sh' --include='*.service' --include='*.toml' --include='*.yaml' --include='*.yml' \
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=data 2>/dev/null | head -420

section "USER SERVICES, TIMERS AND CRON"
systemctl --user list-units --all --no-pager 2>&1 | grep -Ei 'ota|scraper|scheduler|streamlit|vnc' || true
printf '\nInstalled matching user units:\n'
systemctl --user list-unit-files --no-pager 2>&1 | grep -Ei 'ota|scraper|scheduler|streamlit|vnc' || true
printf '\nMatching unit files on disk:\n'
find "$HOME/.config/systemd/user" /etc/systemd/user /etc/systemd/system -maxdepth 3 -type f 2>/dev/null \
  | grep -Ei 'ota|scraper|scheduler|streamlit|vnc' | sort
printf '\nUser crontab:\n'
crontab -l 2>&1

section "PYTHON AND PLAYWRIGHT INSTALLATION"
"$PROJECT/.venv/bin/python" --version 2>&1
"$PROJECT/.venv/bin/streamlit" version 2>&1
"$PROJECT/.venv/bin/python" - <<'PY' 2>&1
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    print('Chromium executable:', p.chromium.executable_path)
    print('Chromium exists:', Path(p.chromium.executable_path).exists())
PY

section "VISIBLE PLAYWRIGHT SMOKE TEST ON DISPLAY :11"
printf 'This uses a brand-new temporary browser profile, opens a test page briefly on :11, saves one screenshot, and closes it.\n'
env \
  DISPLAY="$DISPLAY_ID" \
  XAUTHORITY="$HOME/.Xauthority" \
  XDG_RUNTIME_DIR="$SMOKE_DIR/runtime" \
  WAYLAND_DISPLAY= \
  XDG_SESSION_TYPE=x11 \
  GDK_BACKEND=x11 \
  QT_QPA_PLATFORM=xcb \
  DBUS_SESSION_BUS_ADDRESS= \
  SMOKE_DIR="$SMOKE_DIR" \
  "$PROJECT/.venv/bin/python" - <<'PY' 2>&1
import json, os, traceback
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(os.environ['SMOKE_DIR'])
runtime=root/'runtime'
profile=root/'profile'
runtime.mkdir(parents=True, exist_ok=True)
profile.mkdir(parents=True, exist_ok=True)
os.chmod(runtime, 0o700)
result={'display': os.environ.get('DISPLAY'), 'success': False}
try:
    with sync_playwright() as p:
        result['executable']=p.chromium.executable_path
        context=p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            args=['--disable-dev-shm-usage'],
            viewport={'width': 1100, 'height': 700},
        )
        page=context.pages[0] if context.pages else context.new_page()
        page.set_content('<html><body style="font-family:sans-serif"><h1>Scraper 1 VNC smoke test</h1><p>Visible Playwright can open on DISPLAY :11.</p></body></html>')
        page.wait_for_timeout(1500)
        shot=root/'playwright_visible_smoke.png'
        page.screenshot(path=str(shot))
        result['title']=page.title()
        result['screenshot']=str(shot)
        result['success']=True
        context.close()
except Exception as exc:
    result['error_type']=type(exc).__name__
    result['error']=str(exc)
    result['traceback']=traceback.format_exc()
(root/'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result, indent=2))
raise SystemExit(0 if result['success'] else 20)
PY
SMOKE_RC=$?
printf 'Playwright smoke-test exit code: %s\n' "$SMOKE_RC"

section "CONCISE END SUMMARY"
printf 'Diagnostic report: %s\n' "$OUT"
printf 'Smoke-test folder: %s\n' "$SMOKE_DIR"
printf 'Port-8501 listener count: %s\n' "${#PORT_PIDS[@]}"
if ((${#PORT_PIDS[@]})); then
  printf 'Backend DISPLAY: %s\n' "$(proc_env "${PORT_PIDS[0]}" DISPLAY 2>/dev/null || printf '<unset>')"
  printf 'Backend INSTANCE_ID: %s\n' "$(proc_env "${PORT_PIDS[0]}" INSTANCE_ID 2>/dev/null || printf '<unset>')"
  printf 'Backend DATA: %s\n' "$(proc_env "${PORT_PIDS[0]}" INSTANCE_DATA_DIR 2>/dev/null || printf '<unset>')"
fi
printf 'Visible Playwright smoke-test result: %s\n' "$([[ "$SMOKE_RC" -eq 0 ]] && echo PASSED || echo FAILED)"
printf '\nNothing in the original database, scheduler settings, exports, checkpoints, or status files was deleted or reset.\n'
printf '\nCOPY THE TERMINAL OUTPUT BACK INTO CHAT. The complete report is also saved at:\n%s\n' "$OUT"
