#!/usr/bin/env python3
"""Target Cam launcher: start the updated app from /data, or fall back.

Lives on the read-only root next to the built-in ("legacy") app and is never updated over the air,
so a bad release cannot break it. Order of preference:
  1. APP_ROOT/current   - unless it already failed to start START_ATTEMPTS times in a row
  2. APP_ROOT/previous  - promoted to current when (1) keeps failing
  3. the built-in copy next to this file
The app clears the attempt counter once it serves requests (updater.mark_good).
Only the standard library is used here on purpose.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("APP_ROOT", "/data/app"))
MAX = int(os.environ.get("START_ATTEMPTS", "3"))
REQUIRED = ("app.py", "updater.py", "VERSION")


def log(msg):
    print("launcher: " + msg, file=sys.stderr, flush=True)


def fsync_dir(d):
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def target(name):
    try:
        t = Path(os.readlink(ROOT / name))
    except OSError:
        return None
    t = t if t.is_absolute() else ROOT / t
    return t if all((t / f).is_file() for f in REQUIRED) else None


def relink(name, dest):
    p, tmp = ROOT / name, ROOT / ("." + name + ".tmp")
    if dest is None:
        if os.path.lexists(p):
            p.unlink()
    else:
        if os.path.lexists(tmp):
            tmp.unlink()
        os.symlink(os.path.relpath(dest, ROOT), tmp)
        os.replace(tmp, p)
    fsync_dir(ROOT)


def count_start(rel):
    f = ROOT / "state" / "attempts.json"
    try:
        a = json.loads(f.read_text())
    except (OSError, ValueError):
        a = {}
    n = a.get("count", 0) if a.get("release") == rel.name else 0
    if n >= MAX:
        return False
    write_atomic(f, json.dumps({"release": rel.name, "count": n + 1}))
    return True


def choose():
    """Return (app dir, slot, reason for falling back)."""
    if not ROOT.is_dir():
        return HERE, "legacy", ""
    try:
        cur = target("current")
        if cur is None:
            reason = "installed release is incomplete" if os.path.lexists(ROOT / "current") else ""
            return HERE, "legacy", reason
        if count_start(cur):
            return cur, "current", ""
        reason = "version %s failed to start %d times" % (cur.name, MAX)
        prev = target("previous")
        if prev is not None and prev.resolve() != cur.resolve():
            # promote previous; the failed release is left out and removed by the next update
            relink("current", prev)
            relink("previous", None)
            count_start(prev)
            return prev, "previous", reason
        return HERE, "legacy", reason
    except OSError as e:  # /data read-only after an fs error, etc.
        return HERE, "legacy", "update storage error: %s" % e


def main():
    d, slot, reason = choose()
    if reason:
        log(reason + " -> starting " + slot)
        try:  # shown in the app until the next install / rollback
            write_atomic(ROOT / "state" / "fallback.txt", reason)
        except OSError:
            pass
    log("starting %s from %s" % (slot, d))
    env = dict(os.environ, APP_SLOT=slot, APP_FALLBACK=reason, APP_ROOT=str(ROOT), LEGACY_DIR=str(HERE))
    app = str(d / "app.py")
    os.chdir(d)
    os.execve(sys.executable, [sys.executable, app], env)


if __name__ == "__main__":
    main()
