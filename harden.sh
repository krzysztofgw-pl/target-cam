#!/bin/bash
# Target Cam: make the SD card survive power cuts.
#   sudo bash harden.sh prepare      - data partition, journald, watchdog (root still writable)
#   sudo bash harden.sh enable       - read-only root (overlay in RAM) + read-only /boot/firmware, then reboot
#   sudo bash harden.sh maintenance  - back to a writable system (for updates), then reboot
# Needs an ext4 partition labelled "data" (see instructions).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
U="${SUDO_USER:-$(logname)}"; H=$(getent passwd "$U" | cut -d: -f6)
mode="${1:-}"

add_fstab() { grep -qF "$1" /etc/fstab || echo "$1" >> /etc/fstab; }

prepare() {
  [[ "$(findmnt -no FSTYPE /)" != overlay ]] || { echo "Root is already read-only (overlay). Run: maintenance"; exit 1; }
  [[ -e /dev/disk/by-label/data ]] || { echo "Partition with label 'data' not found - create it first."; exit 1; }

  echo "== /data"
  mkdir -p /data
  add_fstab "LABEL=data /data ext4 defaults,noatime,commit=5,errors=remount-ro,nofail,x-systemd.device-timeout=10 0 2"
  systemctl daemon-reload; mount -a
  findmnt /data >/dev/null || { echo "/data did not mount"; exit 1; }

  echo "== photos -> /data/target"
  systemctl stop target.service 2>/dev/null || true
  mkdir -p /data/target
  if [[ -d "$H/target" && ! -L "$H/target" ]]; then
    cp -a "$H/target/." /data/target/
    mv "$H/target" "$H/target.old"
  fi
  [[ -L "$H/target" ]] || ln -s /data/target "$H/target"
  chown -R "$U:$U" /data/target
  chown -h "$U:$U" "$H/target"
  mkdir -p /data/app          # OTA releases (see updater.py)
  chown "$U:$U" /data/app
  for s in target target-cleanup; do
    mkdir -p "/etc/systemd/system/$s.service.d"
    printf '[Unit]\nRequiresMountsFor=/data\n' > "/etc/systemd/system/$s.service.d/data.conf"
  done

  echo "== tailscale state -> /data/tailscale"
  if command -v tailscaled >/dev/null; then
    systemctl stop tailscaled
    mkdir -p /data/tailscale; chmod 700 /data/tailscale
    if ! findmnt /var/lib/tailscale >/dev/null; then
      cp -a /var/lib/tailscale/. /data/tailscale/
      add_fstab "/data/tailscale /var/lib/tailscale none bind,x-systemd.requires-mounts-for=/data 0 0"
      systemctl daemon-reload; mount /var/lib/tailscale
    fi
    systemctl start tailscaled
  fi

  echo "== journald in RAM, no swap, hardware watchdog"
  mkdir -p /etc/systemd/journald.conf.d /etc/systemd/system.conf.d
  printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=16M\n' > /etc/systemd/journald.conf.d/volatile.conf
  printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' > /etc/systemd/system.conf.d/watchdog.conf
  systemctl disable --now dphys-swapfile 2>/dev/null || true
  systemctl restart systemd-journald
  systemctl daemon-reload
  systemctl start target.service
  echo "Prepared. Check the app and Tailscale, then: sudo bash harden.sh enable"
}

boot_rw() { mount -o remount,rw /boot/firmware 2>/dev/null || true; }
cmdline() { local c=/boot/firmware/cmdline.txt; [[ -f "$c" ]] || c=/boot/cmdline.txt; echo "$c"; }

enable() {
  findmnt /data >/dev/null || { echo "/data not mounted - run prepare first"; exit 1; }
  [[ -L "$H/target" ]] || { echo "photos not moved - run prepare first"; exit 1; }
  [[ "$(findmnt -no FSTYPE /)" != overlay ]] || { echo "Already read-only."; exit 0; }
  boot_rw
  raspi-config nonint enable_overlayfs
  CL=$(cmdline)
  # keep /data and /var/lib/tailscale as real (writable) mounts: no overlay on sub-mounts
  grep -q 'overlayroot=tmpfs:recurse=0' "$CL" || sed -i 's/overlayroot=tmpfs\( \|$\)/overlayroot=tmpfs:recurse=0\1/' "$CL"
  grep -q 'overlayroot=tmpfs:recurse=0' "$CL" || { echo "Could not set recurse=0 in $CL - aborting"; exit 1; }
  raspi-config nonint enable_bootro
  echo "Read-only mode set. Rebooting in 5 s (Ctrl+C to cancel)"; sleep 5; reboot
}

maintenance() {
  boot_rw
  raspi-config nonint disable_bootro 2>/dev/null || true     # may refuse while overlay is active - harmless
  raspi-config nonint disable_overlayfs 2>/dev/null || true
  CL=$(cmdline)
  sed -i -E 's/(^|[[:space:]])overlayroot=[^[:space:]]*//g; s/^[[:space:]]+//' "$CL"   # make sure the overlay is really off
  if grep -q overlayroot "$CL"; then echo "overlayroot still in $CL - aborting"; exit 1; fi
  cat "$CL"
  echo "Writable mode set (check after reboot: findmnt -no FSTYPE /  ->  ext4). Rebooting in 5 s"; sleep 5; reboot
}

case "$mode" in prepare) prepare;; enable) enable;; maintenance) maintenance;; *) sed -n '2,6p' "$0";; esac
