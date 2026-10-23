#!/usr/bin/env bash
# Target Cam installer. Run as the normal user (not root), either from an unpacked release:
#   bash install.sh
# or straight from GitHub, which first downloads the latest release package:
#   curl -fsSL https://github.com/krzysztofgw-pl/target-cam/releases/latest/download/install.sh | bash
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Run as a normal user, not root (the script uses sudo where needed)."
  exit 1
fi

if [[ -z "${BASH_SOURCE[0]:-}" || ! -f "$(dirname "${BASH_SOURCE[0]}")/app.py" ]]; then
  REPO="${TARGET_CAM_REPO:-krzysztofgw-pl/target-cam}"
  PKG="$(mktemp -d)"
  echo "== downloading the latest Target Cam release"
  curl -fsSL "https://github.com/$REPO/releases/latest/download/target-cam.tar.gz" | tar xz -C "$PKG"
  exec bash "$PKG/install.sh"
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(id -un)"
CAM="$HOME/camera"
DATA="$HOME/target"

command -v rpicam-still >/dev/null || { echo "rpicam-still not found - is this Raspberry Pi OS?"; exit 1; }
id -nG | grep -qw video || echo "WARNING: user $USER_NAME is not in the 'video' group (camera access may fail)"

echo "== packages"
sudo apt-get install -y python3-flask python3-pil

echo "== files"
mkdir -p "$CAM/static" "$DATA/prev" "$DATA/thumb"
# ~/camera is the built-in ("legacy") version: on the read-only root it is the fallback for updates
for f in app.py updater.py launcher.py VERSION; do install -m 644 "$SRC/$f" "$CAM/$f"; done
install -m 755 "$SRC/cleanup.sh" "$CAM/cleanup.sh"
install -m 644 "$SRC"/static/* "$CAM/static/"
sed -i 's/\r$//' "$CAM/app.py" "$CAM/updater.py" "$CAM/launcher.py" "$CAM/VERSION" "$CAM/cleanup.sh"   # guard against CRLF from Windows

if findmnt /data >/dev/null; then
  echo "== /data/app (updates)"
  sudo mkdir -p /data/app
  sudo chown "$USER_NAME:$USER_NAME" /data/app
else
  echo "/data not mounted yet - updates become available after: sudo bash ~/harden.sh prepare"
fi
# keep harden.sh in the home directory (replaced on every install), so the
# unpacked source folder can be deleted: sudo bash ~/harden.sh enable|maintenance
install -m 755 "$SRC/harden.sh" "$HOME/harden.sh"
sed -i 's/\r$//' "$HOME/harden.sh"

echo "== systemd units"
for u in target.service target-cleanup.service target-cleanup.timer; do
  sed -e "s|__USER__|$USER_NAME|g" -e "s|__HOME__|$HOME|g" "$SRC/systemd/$u" \
    | sudo tee "/etc/systemd/system/$u" >/dev/null
done

echo "== persistent journal (logs survive a power cut / reboot)"
sudo mkdir -p /var/log/journal
sudo systemctl restart systemd-journald

sudo systemctl daemon-reload
sudo systemctl enable target.service
sudo systemctl restart target.service
sudo systemctl enable --now target-cleanup.timer

echo "== check"
sleep 3
systemctl is-active target.service
curl -fsS -m10 http://127.0.0.1:8080/status || echo "status request failed (modem unreachable is OK here)"
echo
systemctl list-timers target-cleanup.timer --no-pager || true

echo "== tailscale serve"
if command -v tailscale >/dev/null; then
  sudo tailscale serve --bg 8080 || echo "tailscale serve failed - enable HTTPS/MagicDNS in the Tailscale admin panel and retry"
  tailscale serve status || true
else
  echo "tailscale not installed - skipping"
fi
echo "harden.sh is in $HOME. The folder $SRC is no longer needed."
echo "Done."
