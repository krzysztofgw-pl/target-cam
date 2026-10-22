# Target Cam

On-demand photos of a long-range target from a Raspberry Pi 3A+ (IMX708 camera, HiLink LTE modem, access via Tailscale).
The root filesystem is read-only, so unplugging the power bank does not corrupt the SD card.

## Installation (new Pi)

```bash
bash install.sh                 # as the normal user
sudo bash ~/harden.sh prepare   # "data" partition -> /data; photos and /data/app live there
sudo bash ~/harden.sh enable    # read-only root, reboots
```

## Updates

```
/  (read-only)   ~/camera/            built-in ("legacy") version, launcher.py
/data/app/       releases/<version>/  downloaded releases
                 current, previous    symlinks, switched atomically
                 state/               start attempts, last check result
```

- `target.service` runs `launcher.py`, which starts `current`. If a version fails to start
  3 times in a row, the launcher falls back to `previous`, and if that fails too, to the built-in version.
  The app clears the counter as soon as it answers requests.
- A minute after boot the app checks GitHub (over LTE) for the latest release. A new version
  shows up in the device details (signal icon) with an **Install** button. The same panel has
  **Check for update** and **Back to …** (return to the previous or built-in version).
- Installing: download the archive, verify sha256, unpack next to the current one, check syntax,
  switch the symlink and restart the app (~20 s). A power cut at any point leaves either the old or the new version.
  The root stays read-only; `maintenance` mode is not needed.
- The built-in version changes only via `harden.sh maintenance` + `install.sh` + `harden.sh enable`.

From the Pi console: `python3 ~/camera/updater.py status|check|install [file.tar.gz]|rollback|legacy`,
then `sudo systemctl restart target`. `install file.tar.gz` works offline.

An update can only change app files (`app.py`, `updater.py`, `static/`, `cleanup.sh`).
New apt packages, systemd unit changes or a new `launcher.py` need a reinstall with `install.sh`.

### Private repository

The repository is public, so the Pi needs no credentials. If it is ever made private, give the Pi a
read-only token: GitHub → Settings → Developer settings → Fine-grained token, access to `target-cam` only,
permission *Contents: Read-only*. On the Pi:

```bash
echo 'github_pat_...' > /data/app/github_token && chmod 600 /data/app/github_token
```

### Publishing a new version

1. Change `VERSION` (e.g. `1.1.0`) and push / merge it to `main`.
2. The *Release* workflow runs the tests, builds `target-cam-1.1.0.tar.gz` + `.sha256`
   and publishes release `v1.1.0` (the tag is created automatically). The release notes are shown on the Pi before installing.

## Migrating an existing installation

```bash
sudo bash ~/harden.sh maintenance   # writable root, reboots
git clone https://github.com/krzysztofgw-pl/target-cam.git && cd target-cam   # or unpack the ZIP from GitHub
bash install.sh                      # new built-in version + launcher + /data/app
sudo bash ~/harden.sh enable
```
