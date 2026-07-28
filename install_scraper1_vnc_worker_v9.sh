#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT="${OTA_PROJECT_DIR:-/home/jf/Projects/OTA-SCRAPER-LINUX}"
BASE="https://raw.githubusercontent.com/JFMOURIER/OTA-SCRAPER-LINUX/c37237d0b819e4b06bd206ef97a164a19dccddfd/tools/vnc_fix_v9_final"
WORK="$(mktemp -d /tmp/scraper1-vnc-v9.XXXXXX)"
PAYLOAD="$WORK/payload.b64"
INSTALLER="$WORK/install.sh"
EXPECTED_SHA256="a7c6c3d4fb38b9d76076c0e60e4f61450d7c71be006ff7efc247f6d68be0158c"
trap 'rm -rf "$WORK"' EXIT

for part in 00 01 02 03 04 05; do
  curl -fsSL --retry 3 --retry-delay 1 "$BASE/part${part}.b64" >> "$PAYLOAD"
done
base64 --decode "$PAYLOAD" > "$INSTALLER"
printf '%s  %s\n' "$EXPECTED_SHA256" "$INSTALLER" | sha256sum --check --status || {
  echo "ERROR: downloaded VNC repair payload failed its SHA-256 integrity check." >&2
  exit 20
}
bash -n "$INSTALLER"
chmod 700 "$INSTALLER"
OTA_PROJECT_DIR="$PROJECT" "$INSTALLER"
