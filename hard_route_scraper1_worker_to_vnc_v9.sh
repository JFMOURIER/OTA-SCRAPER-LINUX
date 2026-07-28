#!/usr/bin/env bash
# Hard-route the actual near_30_days worker and every browser child to TigerVNC :11.
# Preserves the original database, scheduler configuration, exports, checkpoints and profiles.
set -Eeuo pipefail

PROJECT="${OTA_PROJECT_DIR:-/home/jf/Projects/OTA-SCRAPER-LINUX}"
DATA_DIR="$PROJECT/data/instances/near_30_days"
STATUS_DIR="$DATA_DIR/status"
DISPLAY_ID=":11"
VNC_NUMBER="11"
PORT="8501"
INSTANCE="near_30_days"
UI_UNIT="ota-ui-near.service"
DISPATCH_UNIT="ota-scheduler-dispatch.service"
WORKER_UNIT="ota-scraper-run@near_30_days.service"
LEGACY_WORKER_UNIT="ota-scraper-near.service"
USER_ID="$(id -u)"
USER_NAME="$(id -un)"
USER_RUNTIME="/run/user/$USER_ID"
USER_BUS="unix:path=$USER_RUNTIME/bus"
XAUTHORITY_FILE="$HOME/.Xauthority"
ROUTE_DIR="$PROJECT/vnc_worker_v9"
BIN_DIR="$ROUTE_DIR/bin"
CONFIG_FILE="$ROUTE_DIR/original_worker.json"
WRAPPER="$ROUTE_DIR/vnc_worker_entry.py"
SITE_FILE="$ROUTE_DIR/sitecustomize.py"
NODE_FILE="$ROUTE_DIR/force_display.cjs"
SHELL_ENV="$ROUTE_DIR/force_vnc_env.sh"
STAMP="$(date +%Y%m%d_%H%M%S)"
REPORT="$HOME/Downloads/scraper1_vnc_hard_route_${STAMP}.txt"

mkdir -p "$HOME/Downloads" "$STATUS_DIR" "$ROUTE_DIR" "$BIN_DIR"
exec > >(tee "$REPORT") 2>&1

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
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

listener_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u
  elif command -v fuser >/dev/null 2>&1; then
    fuser -n tcp "$PORT" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u || true
  else
    ss -ltnp "sport = :$PORT" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u
  fi
}

fresh_active_status() {
  local file="$STATUS_DIR/current_job_status.json"
  [[ -f "$file" ]] || return 1
  "$PROJECT/.venv/bin/python" - "$file" <<'PY' >/dev/null 2>&1
from datetime import datetime, timezone
import json, sys
try:
    data=json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(1)
if str(data.get("status") or "").lower() not in {"starting","running","stopping"}:
    raise SystemExit(1)
stamp=data.get("last_updated_at") or data.get("timestamp")
if not stamp:
    raise SystemExit(0)
try:
    dt=datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt=dt.replace(tzinfo=timezone.utc)
    age=(datetime.now(timezone.utc)-dt.astimezone(timezone.utc)).total_seconds()
except Exception:
    raise SystemExit(0)
raise SystemExit(0 if age < 900 else 1)
PY
}

active_scrape_exists() {
  [[ "$(user_systemctl is-active "$WORKER_UNIT" 2>/dev/null || true)" == "active" ]] && return 0
  pgrep -af 'ota_scheduled_run.py.*near_30_days|vnc_worker_entry.py.*near_30_days' >/dev/null 2>&1 && return 0
  fresh_active_status && return 0
  return 1
}

verify_prerequisites() {
  [[ -d "$PROJECT" && -f "$PROJECT/app.py" ]] || die "Project not found at $PROJECT"
  [[ -x "$PROJECT/.venv/bin/python" ]] || die "Project virtual environment is missing."
  [[ -d "$DATA_DIR" ]] || die "Original data directory missing: $DATA_DIR"
  [[ -S "$USER_RUNTIME/bus" ]] || die "User D-Bus socket missing: $USER_RUNTIME/bus"
  command -v xdpyinfo >/dev/null 2>&1 || die "xdpyinfo is missing."
  command -v curl >/dev/null 2>&1 || die "curl is missing."
  DISPLAY="$DISPLAY_ID" XAUTHORITY="$XAUTHORITY_FILE" xdpyinfo >/dev/null 2>&1 || die "TigerVNC display $DISPLAY_ID is not running."
  user_systemctl show-environment >/dev/null 2>&1 || die "The user-systemd bus is not reachable."
  user_systemctl cat "$UI_UNIT" >/dev/null 2>&1 || die "Missing unit: $UI_UNIT"
  user_systemctl cat "$DISPATCH_UNIT" >/dev/null 2>&1 || die "Missing unit: $DISPATCH_UNIT"
  user_systemctl cat "$WORKER_UNIT" >/dev/null 2>&1 || die "Missing unit: $WORKER_UNIT"
  say "Verified TigerVNC $DISPLAY_ID, original instance data and user-systemd."
}

capture_original_worker() {
  if [[ -s "$CONFIG_FILE" ]]; then
    "$PROJECT/.venv/bin/python" - "$CONFIG_FILE" "$WRAPPER" <<'PY' >/dev/null 2>&1 && {
import json, sys
p=sys.argv[1]; wrapper=sys.argv[2]
d=json.load(open(p, encoding="utf-8"))
argv=d.get("argv") or []
assert argv and wrapper not in " ".join(argv)
PY
      say "Reusing the preserved original worker command."
      return 0
    }
  fi

  local fragment
  local working
  fragment="$(user_systemctl show "$WORKER_UNIT" -p FragmentPath --value)"
  working="$(user_systemctl show "$WORKER_UNIT" -p WorkingDirectory --value 2>/dev/null || true)"
  [[ -f "$fragment" ]] || die "Could not locate worker unit file: $fragment"
  [[ -n "$working" && "$working" != "n/a" ]] || working="$PROJECT"

  "$PROJECT/.venv/bin/python" - "$fragment" "$CONFIG_FILE" "$working" "$INSTANCE" "$HOME" "$USER_NAME" "$USER_ID" "$USER_RUNTIME" "$WORKER_UNIT" "$WRAPPER" <<'PY'
from __future__ import annotations
import json, os, re, shlex, sys
from pathlib import Path

unit=Path(sys.argv[1]); target=Path(sys.argv[2]); working=sys.argv[3]
instance, home, user, uid, runtime, unit_name, wrapper=sys.argv[4:11]
text=unit.read_text(encoding="utf-8", errors="replace")
logical=[]; current=""
for raw in text.splitlines():
    stripped=raw.rstrip()
    if current:
        current += stripped
    else:
        current = stripped
    if current.endswith("\\"):
        current=current[:-1]
        continue
    logical.append(current); current=""
if current: logical.append(current)
in_service=False; values=[]
for raw in logical:
    line=raw.strip()
    if line.startswith("[") and line.endswith("]"):
        in_service=line.lower()=="[service]"
        continue
    if in_service and line.startswith("ExecStart="):
        value=line.split("=",1)[1].strip()
        if value: values.append(value)
if not values:
    raise SystemExit(f"No original ExecStart found in {unit}")
value=values[0]
while value and value[0] in "-+!:@":
    value=value[1:]
replacements={
    "%%":"\0PERCENT\0", "%i":instance, "%I":instance, "%h":home,
    "%u":user, "%U":uid, "%t":runtime, "%n":unit_name,
}
for old,new in replacements.items(): value=value.replace(old,new)
value=value.replace("\0PERCENT\0", "%")
argv=shlex.split(value, posix=True)
if not argv or wrapper in " ".join(argv):
    raise SystemExit(f"Unsafe original command: {argv}")
target.write_text(json.dumps({
    "unit_file":str(unit), "working_directory":working, "argv":argv
}, indent=2), encoding="utf-8")
print("Captured original worker command:")
print("  cwd:", working)
print("  argv:", json.dumps(argv))
PY
}

write_shell_environment() {
  cat > "$SHELL_ENV" <<EOF
export DISPLAY='$DISPLAY_ID'
export SCRAPER_EXPECTED_DISPLAY='$DISPLAY_ID'
export OTA_VNC_CONTAINED='1'
export OTA_DISABLE_WORKSPACE_MOVE='1'
export OTA_WORKSPACE_ROUTING='disabled'
export XAUTHORITY='$XAUTHORITY_FILE'
export XDG_RUNTIME_DIR='$USER_RUNTIME'
export DBUS_SESSION_BUS_ADDRESS='$USER_BUS'
export WAYLAND_DISPLAY=''
export XDG_SESSION_TYPE='x11'
export GDK_BACKEND='x11'
export QT_QPA_PLATFORM='xcb'
export OZONE_PLATFORM='x11'
EOF
  chmod 700 "$SHELL_ENV"
}

write_python_guard() {
  cat > "$SITE_FILE" <<'PY'
from __future__ import annotations
import asyncio, os, subprocess
from typing import Any

ENABLED=os.environ.get("OTA_VNC_CONTAINED", "").lower() in {"1","true","yes","on"}
TARGET=os.environ.get("SCRAPER_EXPECTED_DISPLAY", ":11") or ":11"
XAUTH=os.environ.get("XAUTHORITY") or os.path.expanduser("~/.Xauthority")

def forced_env(value=None):
    env=dict(os.environ if value is None else value)
    if ENABLED:
        env.update({
            "DISPLAY":TARGET, "SCRAPER_EXPECTED_DISPLAY":TARGET,
            "OTA_VNC_CONTAINED":"1", "OTA_DISABLE_WORKSPACE_MOVE":"1",
            "OTA_WORKSPACE_ROUTING":"disabled", "XAUTHORITY":XAUTH,
            "WAYLAND_DISPLAY":"", "XDG_SESSION_TYPE":"x11",
            "GDK_BACKEND":"x11", "QT_QPA_PLATFORM":"xcb",
            "OZONE_PLATFORM":"x11",
        })
    return env

if ENABLED:
    os.environ.update(forced_env())
    env_cls=os.environ.__class__
    original_setitem=env_cls.__setitem__
    def guarded_setitem(self, key, value):
        if str(key)=="DISPLAY": value=TARGET
        return original_setitem(self, key, value)
    env_cls.__setitem__=guarded_setitem

    original_popen=subprocess.Popen.__init__
    def guarded_popen(self, *args:Any, **kwargs:Any):
        kwargs["env"]=forced_env(kwargs.get("env"))
        return original_popen(self, *args, **kwargs)
    subprocess.Popen.__init__=guarded_popen

    original_execve=os.execve
    def guarded_execve(path, argv, env): return original_execve(path, argv, forced_env(env))
    os.execve=guarded_execve

    if hasattr(os, "execvpe"):
        original_execvpe=os.execvpe
        def guarded_execvpe(file, args, env): return original_execvpe(file, args, forced_env(env))
        os.execvpe=guarded_execvpe

    original_async_exec=asyncio.create_subprocess_exec
    original_async_shell=asyncio.create_subprocess_shell
    async def guarded_async_exec(*args, **kwargs):
        kwargs["env"]=forced_env(kwargs.get("env")); return await original_async_exec(*args, **kwargs)
    async def guarded_async_shell(*args, **kwargs):
        kwargs["env"]=forced_env(kwargs.get("env")); return await original_async_shell(*args, **kwargs)
    asyncio.create_subprocess_exec=guarded_async_exec
    asyncio.create_subprocess_shell=guarded_async_shell
PY
  "$PROJECT/.venv/bin/python" -m py_compile "$SITE_FILE"
}

write_node_guard() {
  cat > "$NODE_FILE" <<'JS'
'use strict';
const cp = require('child_process');
const target = process.env.SCRAPER_EXPECTED_DISPLAY || ':11';
function forced(env) {
  return Object.assign({}, process.env, env || {}, {
    DISPLAY: target,
    SCRAPER_EXPECTED_DISPLAY: target,
    OTA_VNC_CONTAINED: '1',
    OTA_DISABLE_WORKSPACE_MOVE: '1',
    OTA_WORKSPACE_ROUTING: 'disabled',
    WAYLAND_DISPLAY: '',
    XDG_SESSION_TYPE: 'x11',
    GDK_BACKEND: 'x11',
    QT_QPA_PLATFORM: 'xcb',
    OZONE_PLATFORM: 'x11'
  });
}
Object.assign(process.env, forced(process.env));
const spawn = cp.spawn;
cp.spawn = function(command, args, options) {
  options = Object.assign({}, options || {}, {env: forced(options && options.env)});
  return spawn.call(cp, command, args, options);
};
const spawnSync = cp.spawnSync;
cp.spawnSync = function(command, args, options) {
  options = Object.assign({}, options || {}, {env: forced(options && options.env)});
  return spawnSync.call(cp, command, args, options);
};
const execFile = cp.execFile;
cp.execFile = function(file, args, options, callback) {
  if (typeof args === 'function') return execFile.call(cp, file, [], {env: forced()}, args);
  if (typeof options === 'function') return execFile.call(cp, file, args || [], {env: forced()}, options);
  options = Object.assign({}, options || {}, {env: forced(options && options.env)});
  return execFile.call(cp, file, args || [], options, callback);
};
const fork = cp.fork;
cp.fork = function(modulePath, args, options) {
  options = Object.assign({}, options || {}, {env: forced(options && options.env)});
  return fork.call(cp, modulePath, args || [], options);
};
JS
}

write_x_wrappers() {
  local name
  local real
  for name in wmctrl xdotool xprop xwininfo; do
    real="$(command -v "$name" 2>/dev/null || true)"
    [[ -n "$real" ]] || continue
    real="$(readlink -f "$real")"
    [[ "$real" == "$BIN_DIR/"* ]] && continue
    cat > "$BIN_DIR/$name" <<EOF
#!/usr/bin/env bash
export DISPLAY='$DISPLAY_ID'
export XAUTHORITY='$XAUTHORITY_FILE'
export WAYLAND_DISPLAY=''
exec '$real' "\$@"
EOF
    chmod 700 "$BIN_DIR/$name"
  done
}

write_worker_wrapper() {
  cat > "$WRAPPER" <<'PY'
#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys
from pathlib import Path

root=Path(__file__).resolve().parent
config=json.loads((root/"original_worker.json").read_text(encoding="utf-8"))
argv=list(config["argv"])
working=config.get("working_directory") or str(root.parent)
target=os.environ.get("SCRAPER_EXPECTED_DISPLAY", ":11") or ":11"
user_runtime=os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
node_file=str(root/"force_display.cjs")
site_root=str(root)
bin_root=str(root/"bin")
shell_env=str(root/"force_vnc_env.sh")
project=str(root.parent)

env=dict(os.environ)
env.update({
    "DISPLAY":target,
    "SCRAPER_EXPECTED_DISPLAY":target,
    "OTA_VNC_CONTAINED":"1",
    "OTA_DISABLE_WORKSPACE_MOVE":"1",
    "OTA_WORKSPACE_ROUTING":"disabled",
    "XAUTHORITY":env.get("XAUTHORITY") or str(Path.home()/".Xauthority"),
    "XDG_RUNTIME_DIR":user_runtime,
    "DBUS_SESSION_BUS_ADDRESS":env.get("DBUS_SESSION_BUS_ADDRESS") or f"unix:path={user_runtime}/bus",
    "WAYLAND_DISPLAY":"",
    "XDG_SESSION_TYPE":"x11",
    "GDK_BACKEND":"x11",
    "QT_QPA_PLATFORM":"xcb",
    "OZONE_PLATFORM":"x11",
    "ELECTRON_OZONE_PLATFORM_HINT":"x11",
    "BASH_ENV":shell_env,
    "ENV":shell_env,
})
old_python=env.get("PYTHONPATH", "")
env["PYTHONPATH"]="\n".join([]) if False else ":".join(x for x in [site_root, project, old_python] if x)
old_node=env.get("NODE_OPTIONS", "").strip()
env["NODE_OPTIONS"]=(f"--require={node_file} " + old_node).strip()
env["PATH"]=bin_root + os.pathsep + env.get("PATH", "/usr/local/bin:/usr/bin:/bin")

if len(sys.argv)>1 and sys.argv[1]=="--self-test":
    print(json.dumps({"display":env["DISPLAY"], "argv":argv, "cwd":working, "pythonpath":env["PYTHONPATH"]}, indent=2))
    raise SystemExit(0)

os.chdir(working)
os.execvpe(argv[0], argv, env)
PY
  chmod 700 "$WRAPPER"
  "$PROJECT/.venv/bin/python" -m py_compile "$WRAPPER"
}

write_dropins() {
  local ui_dir="$HOME/.config/systemd/user/${UI_UNIT}.d"
  local dispatch_dir="$HOME/.config/systemd/user/${DISPATCH_UNIT}.d"
  local worker_dir="$HOME/.config/systemd/user/${WORKER_UNIT}.d"
  mkdir -p "$ui_dir" "$dispatch_dir" "$worker_dir"

  cat > "$ui_dir/zz-vnc11-hard-route.conf" <<EOF
[Service]
Environment="DISPLAY=$DISPLAY_ID"
Environment="SCRAPER_EXPECTED_DISPLAY=$DISPLAY_ID"
Environment="XAUTHORITY=$XAUTHORITY_FILE"
Environment="XDG_RUNTIME_DIR=$USER_RUNTIME"
Environment="DBUS_SESSION_BUS_ADDRESS=$USER_BUS"
Environment="WAYLAND_DISPLAY="
Environment="XDG_SESSION_TYPE=x11"
EOF

  cat > "$dispatch_dir/zz-vnc11-hard-route.conf" <<EOF
[Service]
Environment="XDG_RUNTIME_DIR=$USER_RUNTIME"
Environment="DBUS_SESSION_BUS_ADDRESS=$USER_BUS"
EOF

  cat > "$worker_dir/zz-vnc11-hard-route.conf" <<EOF
[Service]
ExecStart=
ExecStart=$PROJECT/.venv/bin/python $WRAPPER
Environment="DISPLAY=$DISPLAY_ID"
Environment="SCRAPER_EXPECTED_DISPLAY=$DISPLAY_ID"
Environment="OTA_VNC_CONTAINED=1"
Environment="OTA_DISABLE_WORKSPACE_MOVE=1"
Environment="OTA_WORKSPACE_ROUTING=disabled"
Environment="XAUTHORITY=$XAUTHORITY_FILE"
Environment="XDG_RUNTIME_DIR=$USER_RUNTIME"
Environment="DBUS_SESSION_BUS_ADDRESS=$USER_BUS"
Environment="WAYLAND_DISPLAY="
Environment="XDG_SESSION_TYPE=x11"
Environment="GDK_BACKEND=x11"
Environment="QT_QPA_PLATFORM=xcb"
Environment="OZONE_PLATFORM=x11"
Environment="BASH_ENV=$SHELL_ENV"
Environment="ENV=$SHELL_ENV"
Environment="PYTHONPATH=$ROUTE_DIR:$PROJECT"
Environment="NODE_OPTIONS=--require=$NODE_FILE"
Environment="PATH=$BIN_DIR:/home/jf/.local/bin:/usr/local/bin:/usr/bin:/bin"
EOF

  if user_systemctl cat "$LEGACY_WORKER_UNIT" >/dev/null 2>&1; then
    local legacy_dir="$HOME/.config/systemd/user/${LEGACY_WORKER_UNIT}.d"
    mkdir -p "$legacy_dir"
    cat > "$legacy_dir/zz-vnc11-hard-route.conf" <<EOF
[Service]
Environment="DISPLAY=$DISPLAY_ID"
Environment="SCRAPER_EXPECTED_DISPLAY=$DISPLAY_ID"
Environment="OTA_VNC_CONTAINED=1"
Environment="OTA_DISABLE_WORKSPACE_MOVE=1"
Environment="XAUTHORITY=$XAUTHORITY_FILE"
Environment="XDG_RUNTIME_DIR=$USER_RUNTIME"
Environment="DBUS_SESSION_BUS_ADDRESS=$USER_BUS"
Environment="WAYLAND_DISPLAY="
Environment="PYTHONPATH=$ROUTE_DIR:$PROJECT"
Environment="NODE_OPTIONS=--require=$NODE_FILE"
Environment="PATH=$BIN_DIR:/home/jf/.local/bin:/usr/local/bin:/usr/bin:/bin"
EOF
  fi

  user_systemctl daemon-reload
}

verify_effective_unit() {
  local exec_value
  local env_value
  exec_value="$(user_systemctl show "$WORKER_UNIT" -p ExecStart --value)"
  env_value="$(user_systemctl show "$WORKER_UNIT" -p Environment --value)"
  [[ "$exec_value" == *"$WRAPPER"* ]] || die "Effective worker ExecStart is not the VNC wrapper: $exec_value"
  [[ "$env_value" == *"DISPLAY=$DISPLAY_ID"* ]] || die "Worker did not load DISPLAY=$DISPLAY_ID"
  [[ "$env_value" == *"OTA_VNC_CONTAINED=1"* ]] || die "Worker containment flag is missing."
  [[ "$env_value" == *"NODE_OPTIONS=--require=$NODE_FILE"* ]] || die "Node/Playwright guard is missing."
  say "Verified effective worker ExecStart: $WRAPPER"
  say "Verified effective worker DISPLAY=$DISPLAY_ID and process guards."
}

restart_ui_and_timer() {
  user_systemctl stop "$DISPATCH_TIMER" >/dev/null 2>&1 || true
}

refresh_services() {
  active_scrape_exists && die "A Scraper 1 run is active. Press Stop, wait for it to finish, then run this command again. Nothing was restarted."
  user_systemctl reset-failed "$UI_UNIT" "$DISPATCH_UNIT" "$WORKER_UNIT" >/dev/null 2>&1 || true
  user_systemctl restart "$UI_UNIT"
  local attempt
  for attempt in $(seq 1 50); do
    if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/_stcore/health" 2>/dev/null | grep -qx ok; then break; fi
    sleep 1
  done
  curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/_stcore/health" 2>/dev/null | grep -qx ok || die "The Scraper 1 UI did not become healthy."
  user_systemctl restart ota-scheduler-dispatch.timer
  [[ "$(user_systemctl is-active ota-scheduler-dispatch.timer 2>/dev/null || true)" == "active" ]] || die "Scheduler dispatch timer is not active."

  local pid
  pid="$(user_systemctl show "$UI_UNIT" -p MainPID --value | tr -d ' ')"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || die "Could not determine UI PID."
  [[ "$(proc_env "$pid" DISPLAY 2>/dev/null || true)" == "$DISPLAY_ID" ]] || die "UI is not on $DISPLAY_ID."
  say "Verified UI and scheduler timer after reload."
}

run_wrapper_self_test() {
  local output
  output="$(env DISPLAY=:0 SCRAPER_EXPECTED_DISPLAY="$DISPLAY_ID" OTA_VNC_CONTAINED=1 XAUTHORITY="$XAUTHORITY_FILE" XDG_RUNTIME_DIR="$USER_RUNTIME" DBUS_SESSION_BUS_ADDRESS="$USER_BUS" PYTHONPATH="$ROUTE_DIR:$PROJECT" NODE_OPTIONS="--require=$NODE_FILE" "$PROJECT/.venv/bin/python" "$WRAPPER" --self-test)"
  [[ "$output" == *'"display": ":11"'* ]] || die "Wrapper self-test did not force DISPLAY=:11: $output"
  say "Wrapper self-test forced DISPLAY=:11."
}

stop_obsolete_vnc1() {
  if command -v tigervncserver >/dev/null 2>&1; then
    tigervncserver -kill :1 >/dev/null 2>&1 || true
  elif command -v vncserver >/dev/null 2>&1; then
    vncserver -kill :1 >/dev/null 2>&1 || true
  fi
  say "Stopped the obsolete experimental TigerVNC :1 session if it was still present."
}

main() {
  say "Scraper 1 hard VNC worker route v9"
  say "Report: $REPORT"
  verify_prerequisites
  active_scrape_exists && die "A Scraper 1 scrape is active. Stop it first; no files or services were changed."
  capture_original_worker
  write_shell_environment
  write_python_guard
  write_node_guard
  write_x_wrappers
  write_worker_wrapper
  write_dropins
  verify_effective_unit
  run_wrapper_self_test
  refresh_services
  stop_obsolete_vnc1
  say ""
  say "SUCCESS: the actual near_30_days worker ExecStart is now the VNC wrapper."
  say "SUCCESS: Python, Node/Playwright, shell helpers and workspace tools are forced to DISPLAY=$DISPLAY_ID."
  say "SUCCESS: the original database, scheduler, exports, checkpoints and profiles were preserved."
  say ""
  say "Refresh http://127.0.0.1:8501 inside the TigerVNC :11 viewer and run one small test."
}

main "$@"
