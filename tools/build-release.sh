#!/usr/bin/env bash
# Build the release archive for the version in VERSION: dist/target-cam-<ver>.tar.gz + .sha256.
# It holds only what a device needs: the app (installed over the air by updater.py) and the
# installer, hardening script, launcher and systemd units (used by install.sh).
# dist/target-cam.tar.gz is the same archive under a fixed name for .../releases/latest/download/.
set -euo pipefail
cd "$(dirname "$0")/.."
V=$(tr -d '[:space:]' < VERSION)
[[ $V =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bad VERSION: $V"; exit 1; }
mkdir -p dist
A=dist/target-cam-$V.tar.gz
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@${SOURCE_DATE_EPOCH:-0}" \
  -czf "$A" app.py updater.py launcher.py VERSION cleanup.sh install.sh harden.sh static systemd
cp "$A" dist/target-cam.tar.gz
(cd dist && sha256sum "target-cam-$V.tar.gz" > "target-cam-$V.tar.gz.sha256")
echo "$A"
