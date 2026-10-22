#!/usr/bin/env bash
# Build the OTA archive for the version in VERSION: dist/target-cam-<ver>.tar.gz + .sha256
set -euo pipefail
cd "$(dirname "$0")/.."
V=$(tr -d '[:space:]' < VERSION)
[[ $V =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bad VERSION: $V"; exit 1; }
mkdir -p dist
A=dist/target-cam-$V.tar.gz
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@${SOURCE_DATE_EPOCH:-0}" \
  -czf "$A" app.py updater.py VERSION cleanup.sh static
(cd dist && sha256sum "target-cam-$V.tar.gz" > "target-cam-$V.tar.gz.sha256")
echo "$A"
