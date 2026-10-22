#!/usr/bin/env python3
"""Target Cam updates: releases from GitHub installed side by side on /data, switched atomically.

Layout (APP_ROOT, default /data/app):
  releases/<version>/   unpacked releases (app.py, updater.py, VERSION, static/, ...)
  current -> releases/X  what launcher.py starts
  previous -> releases/Y the release before, used for rollback / automatic fallback
  state/                 start attempts, last check result
  github_token           optional, read-only token for a private repo

The built-in copy on the read-only root (~/camera) is the "legacy" fallback and is never touched.
A power cut at any point leaves either the old or the new symlink, never a half-installed release.

CLI: python3 updater.py status|check|install [file.tar.gz]|rollback|legacy
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("APP_ROOT", "/data/app"))
REPO = os.environ.get("UPDATE_REPO", "krzysztofgw-pl/target-cam")
API = "https://api.github.com"
KEEP_RELEASES = 2          # current + previous
REQUIRED = ("app.py", "updater.py", "VERSION")
VER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

lock = threading.Lock()    # one check / install / rollback at a time


class UpdateError(Exception):
    pass


# ---------- paths, versions ----------

def releases():
    return ROOT / "releases"


def state():
    return ROOT / "state"


def parse_version(v):
    m = VER_RE.match((v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def read_version(d):
    try:
        return (Path(d) / "VERSION").read_text().strip()
    except OSError:
        return None


def link_target(name):
    """Release dir a symlink in ROOT points to, or None."""
    p = ROOT / name
    try:
        t = Path(os.readlink(p))
    except OSError:
        return None
    t = t if t.is_absolute() else (ROOT / t)
    return t if t.is_dir() else None


def valid_release(d):
    return d is not None and all((d / f).is_file() for f in REQUIRED)


def legacy_dir():
    return Path(os.environ.get("LEGACY_DIR", HERE))


# ---------- durable file operations (power can be cut at any moment) ----------

def fsync_dir(d):
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
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


def set_link(name, target):
    """Atomically point ROOT/name at target (a release dir), or remove it when target is None."""
    p = ROOT / name
    if target is None:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    else:
        tmp = ROOT / ("." + name + ".tmp")
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        os.symlink(os.path.relpath(target, ROOT), tmp)
        os.replace(tmp, p)
    fsync_dir(ROOT)


def fsync_tree(d):
    for dirpath, _, files in os.walk(d):
        for f in files:
            fd = os.open(os.path.join(dirpath, f), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fsync_dir(dirpath)


# ---------- start attempts (shared with launcher.py) ----------

def read_attempts():
    try:
        return json.loads((state() / "attempts.json").read_text())
    except (OSError, ValueError):
        return {}


def mark_good(release):
    """Called by the app once it serves requests: the release started fine."""
    if read_attempts().get("release") == release:
        try:
            (state() / "attempts.json").unlink()
            fsync_dir(state())
        except FileNotFoundError:
            pass


def reset_attempts():
    try:
        (state() / "attempts.json").unlink()
    except FileNotFoundError:
        pass
    try:
        (state() / "fallback.txt").unlink()
    except FileNotFoundError:
        pass
    fsync_dir(state())


# ---------- status ----------

def _read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def status():
    cur, prev = link_target("current"), link_target("previous")
    try:
        last = json.loads((state() / "update.json").read_text())
    except (OSError, ValueError):
        last = {}
    running = read_version(HERE)
    latest = last.get("latest")
    newer = bool(parse_version(latest) and parse_version(running) and parse_version(latest) > parse_version(running))
    return {
        "version": running,
        "slot": os.environ.get("APP_SLOT", "legacy"),
        "fallback": os.environ.get("APP_FALLBACK") or _read(state() / "fallback.txt"),
        "current": read_version(cur) if valid_release(cur) else None,
        "previous": read_version(prev) if valid_release(prev) else None,
        "legacy": read_version(legacy_dir()),
        "latest": latest,
        "notes": last.get("notes", ""),
        "checked_at": last.get("checked_at"),
        "check_error": last.get("error"),
        "update_available": newer,
        "can_update": ROOT.parent.is_dir() and os.access(ROOT if ROOT.exists() else ROOT.parent, os.W_OK),
        "busy": lock.locked(),
    }


# ---------- GitHub ----------

def token():
    t = os.environ.get("GITHUB_TOKEN")
    if t:
        return t.strip()
    try:
        return (ROOT / "github_token").read_text().strip() or None
    except OSError:
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _open(url, accept, auth=True, timeout=20):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "target-cam-updater"})
    t = token() if auth else None
    if t:
        req.add_header("Authorization", "Bearer " + t)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            # asset downloads redirect to a signed URL that must NOT get our token
            return _open(e.headers["Location"], accept, auth=False, timeout=timeout)
        if e.code == 404:
            raise UpdateError("no release found (private repo without github_token?)")
        if e.code in (401, 403):
            raise UpdateError("GitHub refused access (HTTP %d) - check github_token" % e.code)
        raise UpdateError("GitHub HTTP %d" % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError("no connection to GitHub: %s" % getattr(e, "reason", e))


def latest_release():
    with _open("%s/repos/%s/releases/latest" % (API, REPO), "application/vnd.github+json") as r:
        rel = json.load(r)
    tag = rel.get("tag_name", "")
    if not parse_version(tag):
        raise UpdateError("latest release tag %r is not a version" % tag)
    ver = tag.lstrip("v")
    assets = {a["name"]: a for a in rel.get("assets", [])}
    return {"version": ver, "notes": (rel.get("body") or "")[:2000], "assets": assets}


def check():
    """Ask GitHub for the latest release and remember the answer (also the error) for the UI."""
    with lock:
        try:
            rel = latest_release()
            info = {"latest": rel["version"], "notes": rel["notes"], "checked_at": int(time.time())}
        except UpdateError as e:
            info = {"error": str(e), "checked_at": int(time.time())}
            try:  # keep the last known latest version
                old = json.loads((state() / "update.json").read_text())
                info.update({k: old[k] for k in ("latest", "notes") if k in old})
            except (OSError, ValueError, KeyError):
                pass
        if ROOT.is_dir() or ROOT.parent.is_dir():
            try:
                write_atomic(state() / "update.json", json.dumps(info))
            except OSError:
                pass
    s = status()
    if "error" in info:
        raise UpdateError(info["error"])
    return s


def download(url, dest, accept="application/octet-stream"):
    with _open(url, accept, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def install_latest():
    with lock:
        rel = latest_release()
        name = "target-cam-%s.tar.gz" % rel["version"]
        a, s = rel["assets"].get(name), rel["assets"].get(name + ".sha256")
        if not a or not s:
            raise UpdateError("release %s has no %s (+ .sha256)" % (rel["version"], name))
        ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".dl-") as tmp:
            arc, sha = Path(tmp) / name, Path(tmp) / "sha256"
            download(s["url"], sha)
            download(a["url"], arc)
            want = sha.read_text().split()[0].lower()
            if sha256(arc) != want:
                raise UpdateError("checksum mismatch - download damaged, nothing changed")
            return _install(arc, rel["version"])


def install_file(path):
    with lock:
        return _install(Path(path), None)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def _extract(arc, dest):
    with tarfile.open(arc, "r:gz") as t:
        for m in t.getmembers():
            n = os.path.normpath(m.name)
            if n.startswith(("/", "..")) or not (m.isfile() or m.isdir()):
                raise UpdateError("unsafe entry in archive: %s" % m.name)
        for m in t.getmembers():
            out = dest / os.path.normpath(m.name)
            if m.isdir():
                out.mkdir(parents=True, exist_ok=True)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                with t.extractfile(m) as src, open(out, "wb") as f:
                    shutil.copyfileobj(src, f)
                os.chmod(out, 0o755 if m.mode & 0o111 else 0o644)


def _install(arc, expect):
    rel_dir = releases()
    rel_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=rel_dir, prefix=".new-"))
    try:
        try:
            _extract(arc, work)
        except (tarfile.TarError, OSError) as e:
            raise UpdateError("cannot unpack: %s" % e)
        if not valid_release(work):
            raise UpdateError("archive is not a Target Cam release (needs %s)" % ", ".join(REQUIRED))
        ver = read_version(work)
        if not parse_version(ver) or (expect and ver != expect):
            raise UpdateError("bad VERSION %r in archive" % ver)
        for f in ("app.py", "updater.py"):
            try:
                compile((work / f).read_bytes(), str(work / f), "exec")
            except SyntaxError as e:
                raise UpdateError("%s does not compile: %s" % (f, e))
        fsync_tree(work)
        cur = link_target("current")
        final = rel_dir / ver
        if cur is not None and final.resolve() == cur.resolve():
            raise UpdateError("version %s is already installed" % ver)
        if final.exists():
            shutil.rmtree(final)
        os.replace(work, final)
        fsync_dir(rel_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    # switch: previous <- current (if it is a good release), current <- new
    if valid_release(cur):
        set_link("previous", cur)
    set_link("current", final)
    reset_attempts()
    prune()
    return ver


def prune():
    keep = {p.resolve() for p in (link_target("current"), link_target("previous")) if p}
    for d in releases().iterdir():
        if d.is_dir() and d.resolve() not in keep and not d.name.startswith(".new-"):
            shutil.rmtree(d, ignore_errors=True)
    for d in releases().glob(".new-*"):
        shutil.rmtree(d, ignore_errors=True)


def rollback():
    """Swap current and previous."""
    with lock:
        cur, prev = link_target("current"), link_target("previous")
        if not valid_release(prev):
            raise UpdateError("no previous release to go back to")
        set_link("current", prev)
        set_link("previous", cur if valid_release(cur) else None)
        reset_attempts()
        return read_version(prev)


def use_legacy():
    """Start the built-in version from the read-only root; installed releases stay for later."""
    with lock:
        cur = link_target("current")
        if valid_release(cur):  # "back to" then returns to what was running
            set_link("previous", cur)
        set_link("current", None)
        reset_attempts()
        return read_version(legacy_dir())


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    try:
        if cmd == "status":
            r = status()
        elif cmd == "check":
            r = check()
        elif cmd == "install":
            r = {"installed": install_file(argv[2]) if len(argv) > 2 else install_latest()}
        elif cmd == "rollback":
            r = {"current": rollback()}
        elif cmd == "legacy":
            r = {"legacy": use_legacy()}
        else:
            print(__doc__.strip().splitlines()[-1])
            return 2
    except UpdateError as e:
        print("error:", e, file=sys.stderr)
        return 1
    print(json.dumps(r, indent=2))
    if cmd in ("install", "rollback", "legacy"):
        print("restart to apply: sudo systemctl restart target", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
