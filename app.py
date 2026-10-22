#!/usr/bin/env python3
"""Target Cam - on-demand photos of a long-range target (Raspberry Pi + IMX708 + LTE modem)."""
import atexit
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory
from PIL import Image

import updater

BASE = Path(os.environ.get("TARGET_DIR", Path.home() / "target"))
PREV, THUMB, TMP = BASE / "prev", BASE / "thumb", BASE / "tmp"
STATIC = Path(__file__).resolve().parent / "static"
for d in (BASE, PREV, THUMB, TMP):
    d.mkdir(parents=True, exist_ok=True)

# extra rpicam-still options, e.g. "--roi 0.3,0.3,0.4,0.4 --width 1843 --height 1037"
EXTRA = os.environ.get("RPICAM_EXTRA", "").split()
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))
MODEM = os.environ.get("MODEM_URL", "http://192.168.8.1")
HISTORY = int(os.environ.get("HISTORY", "24"))
NAME_RE = re.compile(r"^\d{8}_\d{6}\.jpg$")
CACHE_SECONDS = 86400

app = Flask(__name__)
lock = threading.Lock()   # one capture at a time
plock = threading.Lock()  # preview regeneration


# ---------- files: atomic writes, flushed to the SD card ----------

def fsync_path(path):
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def has_data(path):
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def save_atomic(im, dest, quality):
    # write next to the target in TMP, flush, then rename: a power cut never leaves a half file
    tmp = TMP / (dest.parent.name + "_" + dest.name)
    im.save(tmp, "JPEG", quality=quality)
    fsync_path(tmp)
    os.replace(tmp, dest)
    fsync_path(dest.parent)


def make_previews(src):
    with Image.open(src) as im:
        im.draft("RGB", (1280, 720))  # fast DCT-scaled decode, keeps RAM low on a Pi 3 A+
        im.thumbnail((1280, 1280))
        save_atomic(im, PREV / src.name, 80)
        im.thumbnail((240, 240))
        save_atomic(im, THUMB / src.name, 70)


def remove_photo(name):
    for d in (BASE, PREV, THUMB):
        (d / name).unlink(missing_ok=True)


# ---------- live preview: rpicam-vid MJPEG -> latest frame, polled by the browser ----------
# Low-rate (2 fps) 480p preview for aiming the camera and setting the focus ring by hand.
# The camera can only be used by one process, so a photo (/shot) stops the preview first.

LIVE_W = int(os.environ.get("LIVE_W", "854"))
LIVE_H = int(os.environ.get("LIVE_H", "480"))
LIVE_FPS = os.environ.get("LIVE_FPS", "2")
LIVE_IDLE = float(os.environ.get("LIVE_IDLE", "20"))    # stop when nobody polled for this long [s]
LIVE_MAX = float(os.environ.get("LIVE_MAX", "600"))     # hard limit per start [s] (LTE data / power)
LIVE_POLL = float(os.environ.get("LIVE_POLL", "4"))     # long-poll wait [s]
LIVE_START_TIMEOUT = float(os.environ.get("LIVE_START_TIMEOUT", "12"))
LIVE_EXTRA = os.environ.get("LIVE_EXTRA", "").split()   # e.g. "--buffer-count 3"
WIDE_SENSOR = (2304, 1296)                              # binned, full field of view
ZOOM_SENSORS = [(4608, 2592), (2304, 1296)]             # native 1:1 first, binned as a fallback


def orientation_args(tokens):
    # keep the preview oriented like the photos (flip/rotation taken from RPICAM_EXTRA)
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("--hflip", "--vflip"):
            out.append(t)
        elif t == "--rotation" and i + 1 < len(tokens):
            out += [t, tokens[i + 1]]
            i += 1
        i += 1
    return out


class JpegSplitter:
    """Cuts a concatenated MJPEG byte stream into complete JPEGs.

    Walks the marker structure (so an EXIF thumbnail inside APPn cannot end a frame early)
    and works with any chunking of the input."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        frames = []
        while True:
            n = self._frame_end()
            if n is None:
                break
            if n < 0:
                continue
            frames.append(bytes(self.buf[:n]))
            del self.buf[:n]
        return frames

    def _frame_end(self):
        b = self.buf
        i = b.find(b"\xff\xd8")
        if i < 0:
            if len(b) > 1:
                del b[:-1]              # keep a possible lone trailing 0xFF
            return None
        if i:
            del b[:i]
        n, p = len(b), 2
        while True:
            if p + 2 > n:
                return None
            if b[p] != 0xFF:
                del b[:2]               # corrupt: drop this SOI and resync at the next one
                return -1
            m = b[p + 1]
            if m == 0xFF:               # fill byte
                p += 1
                continue
            if m == 0xD9:
                return p + 2
            if m == 0x00:
                del b[:2]
                return -1
            if m == 0x01 or 0xD0 <= m <= 0xD8:   # standalone markers
                p += 2
                continue
            if p + 4 > n:
                return None
            length = (b[p + 2] << 8) | b[p + 3]
            if length < 2:
                del b[:2]
                return -1
            p += 2 + length
            if m == 0xDA:               # SOS: entropy-coded data follows, up to the next real marker
                q = p
                while True:
                    q = b.find(b"\xff", q)
                    if q < 0 or q + 1 >= n:
                        return None
                    nb = b[q + 1]
                    if nb == 0x00 or 0xD0 <= nb <= 0xD7:
                        q += 2
                        continue
                    p = q
                    break


class LiveError(Exception):
    pass


class Live:
    def __init__(self):
        self.cv = threading.Condition()
        self.proc = None
        self.running = False
        self.frame = None
        self.seq = 0                    # only ever grows, so a client's "after" stays valid across restarts
        self.nbytes = 0
        self.started = 0.0
        self.last_poll = 0.0
        self.stderr_tail = ""
        self.stop_reason = ""

    @staticmethod
    def roi_for(sensor, cx, cy):
        # window of LIVE_W x LIVE_H *sensor* pixels around (cx, cy), as fractions of the full field
        w, h = LIVE_W / sensor[0], LIVE_H / sensor[1]
        x = min(max(cx - w / 2, 0.0), 1.0 - w)
        y = min(max(cy - h / 2, 0.0), 1.0 - h)
        return (x, y, w, h)

    @staticmethod
    def command(sensor, roi):
        cmd = ["rpicam-vid", "-n", "-t", "0", "--codec", "mjpeg", "--flush",
               "-q", "70" if roi else "60",
               "--width", str(LIVE_W), "--height", str(LIVE_H), "--framerate", LIVE_FPS,
               "--mode", "%d:%d" % sensor]
        if roi:
            cmd += ["--roi", ",".join("%.4f" % v for v in roi)]
        cmd += orientation_args(EXTRA) + LIVE_EXTRA + ["-o", "-"]
        return cmd

    def _spawn(self, cmd):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        with self.cv:
            self.proc, self.running, self.frame = proc, True, None
            self.stderr_tail, self.stop_reason, self.nbytes = "", "", 0
            self.started = self.last_poll = time.monotonic()
        threading.Thread(target=self._read_out, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_err, args=(proc,), daemon=True).start()
        return proc

    def _read_out(self, proc):
        splitter = JpegSplitter()
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                for f in splitter.feed(chunk):
                    with self.cv:
                        if self.proc is not proc:
                            return
                        self.frame = f
                        self.seq += 1
                        self.nbytes += len(f)
                        self.cv.notify_all()
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            with self.cv:
                if self.proc is proc:
                    self.running = False
                self.cv.notify_all()

    def _read_err(self, proc):
        try:
            for line in iter(proc.stderr.readline, b""):
                if self.proc is proc:
                    self.stderr_tail = (self.stderr_tail + line.decode(errors="replace"))[-800:]
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass

    @staticmethod
    def _kill(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def stop(self, reason=""):
        with self.cv:
            proc, self.proc, self.running = self.proc, None, False
            if proc and reason:
                self.stop_reason = reason
            self.cv.notify_all()
        if proc:
            self._kill(proc)

    def start(self, mode, cx=0.5, cy=0.5):
        self.stop()
        if mode == "zoom":
            configs = [(s, self.roi_for(s, cx, cy)) for s in ZOOM_SENSORS]
        else:
            configs = [(WIDE_SENSOR, None)]
        problem = ""
        for sensor, roi in configs:
            with self.cv:
                seq0 = self.seq
            proc = self._spawn(self.command(sensor, roi))
            deadline = time.monotonic() + LIVE_START_TIMEOUT
            with self.cv:
                while self.seq == seq0 and self.running and time.monotonic() < deadline:
                    self.cv.wait(0.2)
                got = self.seq > seq0
                tail = self.stderr_tail.strip()
            if got:
                return {"mode": mode, "sensor": "%d:%d" % sensor, "roi": roi,
                        "native": sensor[0] == ZOOM_SENSORS[0][0],
                        "frac": [roi[2], roi[3]] if roi else [1.0, 1.0],
                        "fallback": mode == "zoom" and sensor != ZOOM_SENSORS[0],
                        "w": LIVE_W, "h": LIVE_H, "fps": LIVE_FPS,
                        "max_s": LIVE_MAX, "seq": self.seq}
            problem = tail[-300:] or "no frame within %.0f s" % LIVE_START_TIMEOUT
            self.stop()
        raise LiveError(problem)

    def wait_frame(self, after, timeout):
        end = time.monotonic() + timeout
        with self.cv:
            self.last_poll = time.monotonic()
            while True:
                if not self.running:
                    raise LiveError(self.stop_reason or self.stderr_tail.strip()[-300:] or "preview not running")
                if self.seq > after and self.frame is not None:
                    self.last_poll = time.monotonic()
                    return self.seq, self.frame, self.nbytes
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self.cv.wait(left)

    def watchdog(self):
        while True:
            time.sleep(1.0)
            reason = ""
            with self.cv:
                if self.running:
                    now = time.monotonic()
                    if now - self.last_poll > LIVE_IDLE:
                        reason = "stopped: no viewer"
                    elif now - self.started > LIVE_MAX:
                        reason = "stopped: %g min limit" % (LIVE_MAX / 60)
            if reason:
                self.stop(reason)


live = Live()
threading.Thread(target=live.watchdog, daemon=True).start()
atexit.register(live.stop)


def shoot(shutter=None, gain=None):
    live.stop("stopped: photo taken")   # the camera can only be used by one process
    name = time.strftime("%Y%m%d_%H%M%S") + ".jpg"
    tmp, out = TMP / name, BASE / name
    cmd = ["rpicam-still", "-n", "-q", "95", "-o", str(tmp)]
    if shutter:
        cmd += ["--shutter", str(shutter), "--awb", "daylight", "--immediate"]
        if gain:
            cmd += ["--gain", str(gain)]
    else:
        cmd += ["-t", "1500"]
    cmd += EXTRA
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if r.returncode or not has_data(tmp):
            raise RuntimeError((r.stderr or "no output file")[-300:])
        fsync_path(tmp)
        os.replace(tmp, out)
        fsync_path(BASE)
        make_previews(out)
    except Exception:
        tmp.unlink(missing_ok=True)
        remove_photo(name)
        raise
    return name


def preview_file(folder, name):
    # rebuild a missing/empty preview on demand (e.g. after a power cut); 404 if the source is bad
    if not NAME_RE.match(name):
        abort(404)
    if not has_data(folder / name):
        if not has_data(BASE / name):
            abort(404)
        with plock:
            if not has_data(folder / name):
                try:
                    make_previews(BASE / name)
                except Exception:
                    abort(404)
    return send_from_directory(folder, name, max_age=CACHE_SECONDS)


def cleanup_startup():
    # leftovers of interrupted captures and 0-byte files after a power cut
    for f in TMP.glob("*"):
        f.unlink(missing_ok=True)
    for f in BASE.glob("*.jpg"):
        if not has_data(f):
            remove_photo(f.name)
    for folder in (PREV, THUMB):
        for f in folder.glob("*.jpg"):
            if not has_data(f) or not (BASE / f.name).exists():
                f.unlink(missing_ok=True)


# ---------- status: modem (HiLink API) + Pi ----------

def modem_get(path, cookie=None):
    req = urllib.request.Request(MODEM + path)
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=3) as r:
        return ET.fromstring(r.read())


def xml_dict(root):
    return {c.tag: (c.text or "").strip() for c in root}


def modem_info():
    try:
        cookie = None
        try:  # some firmwares want a session cookie even for GET
            cookie = xml_dict(modem_get("/api/webserver/SesTokInfo")).get("SesInfo")
        except Exception:
            pass
        st = xml_dict(modem_get("/api/monitoring/status", cookie))
        sig = xml_dict(modem_get("/api/device/signal", cookie))
        try:
            plmn = xml_dict(modem_get("/api/net/current-plmn", cookie))
        except Exception:
            plmn = {}
        if "code" in st and len(st) <= 2:  # <error><code>..</code></error>
            return {"error": "modem API error " + st.get("code", "?")}
        nt = st.get("CurrentNetworkType", "")
        return {
            "connected": st.get("ConnectionStatus") == "901",
            "sim": st.get("SimStatus"),
            "net_type": "LTE" if nt in ("19", "101") else ("type " + nt if nt else ""),
            "operator": plmn.get("FullName") or plmn.get("ShortName") or "",
            "rsrp": sig.get("rsrp"), "rsrq": sig.get("rsrq"),
            "sinr": sig.get("sinr"), "rssi": sig.get("rssi"),
            "band": sig.get("band"), "cell_id": sig.get("cell_id"),
        }
    except Exception as e:
        return {"error": str(e)}


def pi_info():
    d = {}
    try:
        d["temp_c"] = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except Exception:
        pass
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3)
        d["throttled"] = r.stdout.strip().split("=")[-1]
    except Exception:
        pass
    try:
        d["uptime_min"] = int(float(Path("/proc/uptime").read_text().split()[0]) // 60)
    except Exception:
        pass
    try:
        d["disk_free_gb"] = round(shutil.disk_usage(BASE).free / 1e9, 1)
        d["photos"] = sum(1 for p in BASE.glob("*.jpg") if NAME_RE.match(p.name))
    except Exception:
        pass
    return d


def no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/status")
def status():
    return no_store(jsonify(modem=modem_info(), pi=pi_info(), sw=updater.status()))


# ---------- updates (see updater.py; installing is always a manual tap) ----------

UPDATE_CHECK = os.environ.get("UPDATE_CHECK", "1") != "0"


def restart_soon():
    # systemd (Restart=always) starts launcher.py again, which picks the new current release
    def go():
        time.sleep(1.5)
        live.stop("restart")
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()


def update_call(fn, *args):
    if updater.lock.locked():
        return jsonify(error="update already running"), 409
    try:
        return no_store(jsonify(result=fn(*args), sw=updater.status()))
    except updater.UpdateError as e:
        return no_store(jsonify(error=str(e), sw=updater.status())), 502
    except OSError as e:
        return no_store(jsonify(error="storage: %s" % e, sw=updater.status())), 500


@app.get("/version")
def version():
    return no_store(jsonify(updater.status()))


@app.post("/update/check")
def update_check():
    return update_call(updater.check)


@app.post("/update/install")
def update_install():
    r = update_call(updater.install_latest)
    if isinstance(r, tuple):
        return r
    restart_soon()
    return r


@app.post("/update/rollback")
def update_rollback():
    to = (request.get_json(silent=True) or {}).get("to")
    r = update_call(updater.use_legacy if to == "legacy" else updater.rollback)
    if isinstance(r, tuple):
        return r
    restart_soon()
    return r


def background_update_tasks():
    # 1) tell the launcher this release starts fine (clears its start-attempt counter)
    rel = Path(__file__).resolve().parent.name
    for _ in range(30):
        time.sleep(2)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/version" % PORT, timeout=5):
                pass
        except Exception:
            continue
        if os.environ.get("APP_SLOT") in ("current", "previous"):
            updater.mark_good(rel)
        break
    # 2) check for a new version once after boot; LTE and the clock (TLS) may need a while
    if not UPDATE_CHECK:
        return
    time.sleep(60)
    for _ in range(12):
        try:
            updater.check()
            return
        except Exception as e:
            print("update check:", e, flush=True)
        time.sleep(600)


# ---------- photos ----------

@app.post("/shot")
def shot():
    if not lock.acquire(blocking=False):
        return jsonify(error="camera busy"), 409
    try:
        d = request.get_json(silent=True) or {}
        shutter = int(d.get("shutter") or 0) or None
        gain = float(d.get("gain") or 0) or None
        return jsonify(name=shoot(shutter, gain))
    except Exception as e:
        return jsonify(error=str(e)), 500
    finally:
        lock.release()


@app.post("/delete/<name>")
def delete(name):
    if not NAME_RE.match(name):
        abort(400)
    remove_photo(name)
    return jsonify(ok=True)


@app.post("/live/start")
def live_start():
    if not lock.acquire(blocking=False):
        return jsonify(error="camera busy"), 409
    try:
        d = request.get_json(silent=True) or {}
        mode = "zoom" if d.get("mode") == "zoom" else "wide"
        cx = min(max(float(d.get("cx", 0.5)), 0.0), 1.0)
        cy = min(max(float(d.get("cy", 0.5)), 0.0), 1.0)
        return no_store(jsonify(live.start(mode, cx, cy)))
    except Exception as e:
        return jsonify(error=str(e)), 500
    finally:
        lock.release()


@app.get("/live/frame")
def live_frame():
    try:
        after = int(request.args.get("after", 0))
    except ValueError:
        after = 0
    try:
        r = live.wait_frame(after, LIVE_POLL)
    except LiveError as e:
        return no_store(jsonify(error=str(e), stopped=True)), 409
    if r is None:
        return no_store(app.response_class(b"", status=204))
    seq, frame, nbytes = r
    resp = app.response_class(frame, mimetype="image/jpeg")
    resp.headers["X-Seq"] = str(seq)
    resp.headers["X-Bytes"] = str(nbytes)
    return no_store(resp)


@app.post("/live/stop")
def live_stop():
    live.stop("stopped")
    return jsonify(ok=True)


@app.get("/list")
def list_():
    names = [p.name for p in BASE.glob("*.jpg") if NAME_RE.match(p.name) and has_data(p)]
    return no_store(jsonify(sorted(names, reverse=True)[:HISTORY]))


@app.get("/f/<name>")
def full(name):
    if not NAME_RE.match(name) or not has_data(BASE / name):
        abort(404)
    return send_from_directory(BASE, name, max_age=CACHE_SECONDS)


@app.get("/p/<name>")
def prev(name):
    return preview_file(PREV, name)


@app.get("/t/<name>")
def thumb(name):
    return preview_file(THUMB, name)


# ---------- PWA ----------

MANIFEST = {
    "id": "/",
    "name": "Target Cam",
    "short_name": "Target Cam",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0c2038",
    "theme_color": "#0c2038",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}


@app.get("/manifest.webmanifest")
def manifest():
    r = jsonify(MANIFEST)
    r.mimetype = "application/manifest+json"
    return r


@app.get("/icon-<name>.png")
def icon(name):
    return send_from_directory(STATIC, "icon-" + name + ".png", max_age=CACHE_SECONDS)


PAGE = r"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Target Cam</title>
<meta name=theme-color content="#0c2038">
<link rel=manifest href=/manifest.webmanifest>
<link rel=icon href=/icon-192.png type=image/png>
<link rel=apple-touch-icon href=/icon-apple.png>
<style>
:root{--bg:#0b111b;--hd:#0c2038;--card:#121c2b;--line:#243650;--btn:#16314f;--txt:#e8eef7;--mut:#7d8da3;--ok:#3ddc97;--acc:#2fbf84;--warn:#ffb020;--bad:#ff6b78}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding-bottom:calc(104px + env(safe-area-inset-bottom))}
button,a{font:inherit;color:inherit;border:0;cursor:pointer;text-decoration:none}
button:disabled{opacity:.6;cursor:default}
svg.i{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;display:block}
header{position:sticky;top:0;z-index:20;background:var(--hd);padding:calc(10px + env(safe-area-inset-top)) 14px 10px;display:flex;align-items:center;gap:10px}
header h1{flex:1;margin:0;font-size:18px;font-weight:650;letter-spacing:.2px}
#logo{width:30px;height:30px;border-radius:7px;display:block}
.ic{width:40px;height:40px;border-radius:12px;background:#16314f;display:grid;place-items:center;position:relative}
.ic.on{background:#1d406a}
.bars{display:flex;gap:2.5px;align-items:flex-end;height:17px}
.bars i{display:block;width:4px;border-radius:1px;background:#3a526f}
.bars i.on{background:var(--ok)}
.ic.q2 .bars i.on{background:var(--warn)}
.ic.q1 .bars i.on,.ic.q0 .bars i.on{background:var(--bad)}
.badge{position:absolute;top:5px;right:5px;width:9px;height:9px;border-radius:50%;border:2px solid #16314f;background:var(--warn)}
.badge.bad{background:var(--bad)}.badge.off{background:#8393a8}
.wrap{max-width:960px;margin:0 auto;padding:0 12px}
#stage{position:relative;margin-top:12px;border-radius:16px;overflow:hidden;background:#000;aspect-ratio:16/9;touch-action:pan-y;user-select:none;-webkit-user-select:none}
#main{width:100%;height:100%;object-fit:contain;display:block;opacity:0;transition:opacity .2s;-webkit-user-drag:none}
#main.ok{opacity:1}
#stage.ld::after{content:"";position:absolute;left:50%;top:50%;width:30px;height:30px;margin:-15px;border:3px solid #ffffff30;border-top-color:#fff;border-radius:50%;animation:sp .8s linear infinite}
#empty[hidden]{display:none}
#empty{position:absolute;inset:0;display:grid;place-items:center;color:var(--mut);text-align:center;padding:20px}
.tag{position:absolute;bottom:10px;background:#000a;padding:4px 10px;border-radius:9px;font-size:12.5px;backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px)}
#stamp{left:10px}#cnt{right:10px}
.nav{position:absolute;top:50%;transform:translateY(-50%);width:38px;height:38px;border-radius:50%;background:#0008;display:grid;place-items:center}
#prv{left:8px}#nxt{right:8px}
@media (hover:none){.nav{display:none}}
.grp{margin-top:18px}
.glab{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);margin:0 2px 8px}
.strip{display:flex;gap:8px;overflow-x:auto;position:relative;padding:2px;scrollbar-width:thin}
.th{flex:0 0 136px;aspect-ratio:16/10;border-radius:11px;overflow:hidden;position:relative;background:#18283b;padding:0;outline:2px solid transparent;outline-offset:-2px}
.th img{width:100%;height:100%;object-fit:cover;display:block}
.th.sel{outline-color:var(--ok)}
.th span{position:absolute;left:5px;bottom:4px;font-size:11px;background:#000a;padding:1px 6px;border-radius:6px}
.th.bad{background:#522}
@media (max-width:640px){.strip{display:grid;grid-template-columns:repeat(3,1fr);overflow:visible}.th{flex:none;width:auto}}
#bar{position:fixed;left:0;right:0;bottom:0;z-index:20;padding:18px 12px calc(14px + env(safe-area-inset-bottom));background:linear-gradient(#0b111b00,#0b111b 38%)}
#barin{max-width:760px;margin:0 auto;display:flex;gap:10px;align-items:center}
.sq{width:56px;height:56px;border-radius:16px;background:var(--btn);display:grid;place-items:center;position:relative;flex:none}
.sq .dot{position:absolute;top:8px;right:8px;width:9px;height:9px;border-radius:50%;background:var(--warn);border:2px solid var(--btn)}
.dng{background:#3a1c22;color:#ff9aa4;transition:width .2s,background .2s}
.dng.arm{width:104px;background:#b3263a;color:#fff;gap:6px;display:flex;align-items:center;justify-content:center;font-weight:650}
.dng.arm svg{flex:none;width:20px;height:20px}
#go{flex:1;height:56px;border-radius:16px;background:var(--acc);color:#04271a;font-size:17px;font-weight:700;letter-spacing:.3px;display:flex;gap:10px;align-items:center;justify-content:center}
#go .rec{width:12px;height:12px;border-radius:50%;background:#04271a}
#go.busy .rec{width:16px;height:16px;border-radius:50%;background:none;border:3px solid #04271a55;border-top-color:#04271a;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
#pop{position:fixed;z-index:40;right:10px;top:calc(60px + env(safe-area-inset-top));width:min(330px,calc(100vw - 20px));background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 16px;box-shadow:0 14px 40px #000b;font-size:13.5px}
#pop[hidden],#sheet[hidden],#ov[hidden]{display:none}
#pop::before{content:"";position:absolute;top:-7px;right:var(--ax,28px);width:12px;height:12px;background:var(--card);border-left:1px solid var(--line);border-top:1px solid var(--line);transform:rotate(45deg)}
#pop h4{margin:0 0 6px;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);font-weight:600}
#pop .r{display:flex;justify-content:space-between;gap:12px;padding:3px 0}
#pop .r b{font-weight:600;text-align:right}
#pop hr{border:0;border-top:1px solid var(--line);margin:10px 0}
#pop .warn{color:var(--warn)}#pop .bad{color:var(--bad)}#pop .ok{color:var(--ok)}
#iss{margin:0 0 10px;padding:8px 10px;border-radius:10px;background:#2a2110;color:var(--warn);font-size:12.5px;line-height:1.45}
#iss.bad{background:#2c1218;color:var(--bad)}
#pf{display:flex;justify-content:space-between;align-items:center;margin-top:10px;color:var(--mut);font-size:12px}
#prf{background:var(--btn);border-radius:9px;padding:5px 12px;font-size:12.5px}
#pop .ua{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
#pop .ua button{background:var(--btn);border-radius:9px;padding:5px 12px;font-size:12.5px}
#pop .ua button.go{background:var(--acc);color:#04271a;font-weight:650}
#pop .note{color:var(--mut);font-size:12px;margin-top:4px;white-space:pre-line;max-height:7em;overflow:auto}
#sheet{position:fixed;z-index:30;left:0;right:0;bottom:calc(92px + env(safe-area-inset-bottom));padding:0 12px}
#sheet>div{max-width:760px;margin:0 auto;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px 16px;box-shadow:0 14px 40px #000b}
#sheet h4{margin:0 0 10px;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);font-weight:600}
#sheet .f{display:flex;gap:10px}
#sheet label{flex:1;font-size:12px;color:var(--mut)}
#sheet input{width:100%;margin-top:4px;font:inherit;font-size:16px;color:var(--txt);background:#0b111b;border:1px solid var(--line);border-radius:10px;padding:.55em .7em}
#sheet .a{display:flex;gap:10px;margin-top:12px;align-items:center}
#sheet .a span{flex:1;font-size:12px;color:var(--mut)}
#sheet .a button{background:var(--btn);border-radius:10px;padding:.5em 1em}
#toast{position:fixed;left:50%;bottom:calc(104px + env(safe-area-inset-bottom));transform:translate(-50%,10px);z-index:50;max-width:min(92vw,520px);background:#e8eef7;color:#0b111b;padding:9px 16px;border-radius:12px;font-size:13.5px;font-weight:600;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;text-align:center}
#toast.show{opacity:1;transform:translate(-50%,0)}
#toast.err{background:#ffd6db;color:#5a0f1b}
#ov{position:fixed;inset:0;background:#000;z-index:60;display:flex;flex-direction:column}
#ovbar{display:flex;gap:10px;align-items:center;padding:calc(8px + env(safe-area-inset-top)) 12px 8px;font:13px ui-monospace,monospace;color:#bbb}
#ovbar button,#ovsave{background:var(--btn);border-radius:10px;padding:.5em .9em;color:var(--txt)}
#ovsave{color:var(--ok)}
#ovp{width:100%;height:6px;accent-color:var(--acc)}
#ovbox{flex:1;overflow:hidden;position:relative;touch-action:none}
#ovimg{position:absolute;left:0;top:0;transform-origin:0 0;max-width:none;user-select:none;-webkit-user-drag:none}
@media (prefers-reduced-motion:reduce){*{animation-duration:.01s!important;transition:none!important}}
</style>
<header>
<img id=logo src=/icon-192.png width=30 height=30 alt="">
<h1>Target Cam</h1>
<button id=sig class=ic aria-label="Connection and device details" title="Connection and device details"><div class=bars><i style=height:5px></i><i style=height:8px></i><i style=height:12px></i><i style=height:17px></i></div><span id=badge class=badge hidden></span></button>
</header>
<div id=pop hidden role=dialog aria-label="Connection and device details"></div>
<div class=wrap>
<div id=stage class=ld>
<img id=main alt="">
<div id=empty hidden>No photos yet.<br>Press PHOTO to take one.</div>
<button id=prv class=nav aria-label="Newer photo"><svg class=i viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg></button>
<button id=nxt class=nav aria-label="Older photo"><svg class=i viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg></button>
<div id=stamp class=tag hidden></div><div id=cnt class=tag hidden></div>
</div>
<div id=gal></div>
</div>
<div id=sheet hidden><div>
<h4>Manual exposure</h4>
<div class=f><label>Shutter (µs)<input id=sh type=number inputmode=numeric min=1 placeholder="auto"></label>
<label>Gain<input id=ga type=number inputmode=decimal step=0.1 min=1 placeholder="auto"></label></div>
<div class=a><span>Empty = automatic exposure.</span><button id=exauto>Auto</button><button id=exdone>Done</button></div>
</div></div>
<div id=toast role=status aria-live=polite></div>
<nav id=bar><div id=barin>
<button id=ex class=sq aria-label="Manual exposure" title="Manual exposure"><svg class=i viewBox="0 0 24 24"><path d="M4 7h8M18 7h2M4 12h2M12 12h8M4 17h10M20 17h0"/><circle cx=15 cy=7 r=2.2 /><circle cx=9 cy=12 r=2.2 /><circle cx=17 cy=17 r=2.2 /></svg><i id=exdot class=dot hidden></i></button>
<button id=go><span class=rec></span><span id=golab>PHOTO</span></button>
<a id=lv class=sq href=/live aria-label="Live view" title="Live view"><svg class=i viewBox="0 0 24 24"><rect x=3 y=6 width=12 height=12 rx=2.5 /><path d="M15 10.5l6-3v9l-6-3z"/></svg></a>
<button id=del class="sq dng" aria-label="Delete photo" title="Delete photo"><svg class=i viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg><span id=dellab hidden>Delete?</span></button>
</div></nav>
<div id=ov hidden>
<div id=ovbar><button id=ovclose>Close</button><a id=ovsave>Save</a><button id=zout>&minus;</button><button id=zin>+</button><span id=ovtxt></span></div>
<progress id=ovp max=1></progress>
<div id=ovbox><img id=ovimg></div>
</div>
<script>
const $=id=>document.getElementById(id);
let cur=null,list=[],ctl=null,blobUrl=null;
const V={s:1,x:0,y:0,min:1,max:1,nw:0,nh:0};
const DAYS=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'],MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const p2=n=>String(n).padStart(2,'0');
function parseName(n){const m=/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.jpg$/.exec(n);return m?new Date(+m[1],m[2]-1,+m[3],+m[4],+m[5],+m[6]):null}
const dayLabel=d=>DAYS[d.getDay()]+' '+d.getDate()+' '+MON[d.getMonth()];
function groupLabel(d){
  const t=new Date(),y=new Date(t.getFullYear(),t.getMonth(),t.getDate()-1);
  const same=(a,b)=>a.getFullYear()===b.getFullYear()&&a.getMonth()===b.getMonth()&&a.getDate()===b.getDate();
  return same(d,t)?'Today':same(d,y)?'Yesterday':dayLabel(d);
}
let tt=null;
function toast(msg,err,ms){
  const t=$('toast');t.textContent=msg;t.className='show'+(err?' err':'');
  clearTimeout(tt);tt=setTimeout(()=>{t.className=''},ms||(err?6000:2200));
}

// ---- gallery ----
function stripOf(el){return el.parentNode}
function centerInStrip(th){
  const s=stripOf(th);
  if(s.scrollWidth>s.clientWidth+2)s.scrollTo({left:th.offsetLeft-(s.clientWidth-th.offsetWidth)/2,behavior:'smooth'});
}
function mark(){
  document.querySelectorAll('.th').forEach(b=>b.classList.toggle('sel',b.dataset.n===cur));
}
function show(n,fromNav){
  cur=n;const i=list.indexOf(n),d=parseName(n);
  const im=$('main');im.classList.remove('ok');$('stage').classList.add('ld');im.src='/p/'+n;
  $('empty').hidden=true;
  $('stamp').hidden=$('cnt').hidden=!d;
  if(d){$('stamp').textContent=dayLabel(d)+' · '+p2(d.getHours())+':'+p2(d.getMinutes());$('cnt').textContent=(i+1)+' / '+list.length}
  mark();
  if(fromNav){const th=document.querySelector('.th.sel');if(th)centerInStrip(th)}
}
function step(dir){
  if(!list.length)return;
  const i=Math.min(list.length-1,Math.max(0,list.indexOf(cur)+dir));
  if(list[i]!==cur)show(list[i],true);
}
function noPhotos(){
  cur=null;$('main').classList.remove('ok');$('main').removeAttribute('src');$('stage').classList.remove('ld');
  $('empty').hidden=false;$('stamp').hidden=$('cnt').hidden=true;
}
async function gal(){
  const l=await (await fetch('/list')).json();
  list=l;
  const g=$('gal');g.innerHTML='';
  let lastKey=null,strip=null;
  l.forEach(n=>{
    const d=parseName(n);const key=d?n.slice(0,8):'?';
    if(key!==lastKey){
      const grp=document.createElement('div');grp.className='grp';
      const lab=document.createElement('div');lab.className='glab';lab.textContent=d?groupLabel(d):'Other';
      strip=document.createElement('div');strip.className='strip';
      grp.append(lab,strip);g.append(grp);lastKey=key;
    }
    const b=document.createElement('button');b.className='th';b.dataset.n=n;
    const i=new Image();i.src='/t/'+n;i.alt='';i.loading='lazy';
    i.onerror=()=>{b.classList.add('bad');i.remove()};
    b.append(i);
    if(d){const s=document.createElement('span');s.textContent=p2(d.getHours())+':'+p2(d.getMinutes());b.append(s)}
    b.onclick=()=>show(n,false);
    strip.append(b);
  });
  if(!l.length)noPhotos();
  else if(!cur||!l.includes(cur))show(l[0],false);
  else{mark();$('cnt').textContent=(l.indexOf(cur)+1)+' / '+l.length}
}

// ---- status icon + details ----
function bars(rsrp){const x=parseFloat(rsrp);if(isNaN(x))return 0;return x>=-85?4:x>=-95?3:x>=-105?2:x>=-115?1:0}
const QL=['no signal','poor','fair','good','excellent'];
function thr(h){
  const v=parseInt(h,16),now=[],seen=[];
  if(isNaN(v))return{now,seen};
  if(v&1)now.push('under-voltage');if(v&2)now.push('frequency capped');if(v&4)now.push('throttled');if(v&8)now.push('temperature limit');
  if(v&0x10000)seen.push('under-voltage');if(v&0x20000)seen.push('frequency capped');if(v&0x40000)seen.push('throttled');if(v&0x80000)seen.push('temperature limit');
  return{now,seen};
}
function fmtUp(min){
  if(min==null)return'?';
  if(min<60)return min+' min';
  if(min<1440)return Math.floor(min/60)+' h '+(min%60)+' min';
  return Math.floor(min/1440)+' d '+Math.floor(min%1440/60)+' h';
}
function assess(j){
  const m=j.modem||{},p=j.pi||{},issues=[];let lvl='ok',nb=0;
  const worse=l=>{if(l==='bad'||(l==='warn'&&lvl==='ok'))lvl=l};
  if(m.error){worse('bad');issues.push('Modem: '+m.error)}
  else{nb=bars(m.rsrp);if(!m.connected){worse('bad');issues.push('Modem is not connected')}}
  const t=thr(p.throttled);
  if(t.now.length){worse('bad');issues.push('Power now: '+t.now.join(', '))}
  else if(t.seen.length){worse('warn');issues.push('Power since boot: '+t.seen.join(', '))}
  if(p.temp_c>=80){worse('bad');issues.push('Temperature '+p.temp_c+' °C')}
  else if(p.temp_c>=70){worse('warn');issues.push('Temperature '+p.temp_c+' °C')}
  if(p.disk_free_gb!=null&&p.disk_free_gb<2){worse('warn');issues.push('Low disk space')}
  const w=j.sw||{};
  if(w.fallback){worse('warn');issues.push('Fallback: '+w.fallback+' (running '+w.slot+' '+(w.version||'?')+')')}
  return{nb,lvl,issues,t};
}
let last=null,lastAt=0;
function paintIcon(a){
  const b=$('sig'),bs=b.querySelectorAll('.bars i');
  b.className='ic'+(a?' q'+a.nb:'')+($('pop').hidden?'':' on');
  bs.forEach((x,i)=>x.classList.toggle('on',!!a&&i<a.nb));
  const bd=$('badge');
  if(!a){bd.hidden=false;bd.className='badge off'}
  else if(a.lvl==='ok')bd.hidden=true;
  else{bd.hidden=false;bd.className='badge'+(a.lvl==='bad'?' bad':'')}
}
function row(l,v,cls){
  const r=document.createElement('div');r.className='r';
  const a=document.createElement('span');a.textContent=l;
  const b=document.createElement('b');b.textContent=v;if(cls)b.className=cls;
  r.append(a,b);return r;
}
function paintPop(){
  const P=$('pop');P.replaceChildren();
  if(!last){
    const d=document.createElement('div');d.className='r';d.textContent='No connection to the device.';P.append(d);
  }else{
    const j=last,m=j.modem||{},p=j.pi||{},a=assess(j);
    if(a.issues.length){const d=document.createElement('div');d.id='iss';if(a.lvl==='bad')d.className='bad';d.textContent=a.issues.join(' · ');P.append(d)}
    const h1=document.createElement('h4');h1.textContent=m.error?'Modem':'LTE'+(m.operator?' · '+m.operator:'');P.append(h1);
    if(!m.error){
      P.append(row('Status',m.connected?'Connected '+(m.net_type||''):'DISCONNECTED',m.connected?'ok':'bad'));
      P.append(row('Signal (RSRP)',(m.rsrp||'?')+' · '+QL[a.nb],a.nb>=3?'ok':a.nb===2?'warn':'bad'));
      P.append(row('SINR / RSRQ',(m.sinr||'?')+' / '+(m.rsrq||'?')));
      P.append(row('Band',m.band?'B'+m.band:'?'));
    }
    P.append(document.createElement('hr'));
    const h2=document.createElement('h4');h2.textContent='Device';P.append(h2);
    P.append(row('Temperature',(p.temp_c??'?')+' °C',p.temp_c>=80?'bad':p.temp_c>=70?'warn':''));
    P.append(row('Uptime',fmtUp(p.uptime_min)));
    P.append(row('Power',a.t.now.length?a.t.now.join(', '):a.t.seen.length?'OK now · '+a.t.seen[0]+' seen':(p.throttled?'OK':'?'),a.t.now.length?'bad':a.t.seen.length?'warn':'ok'));
    P.append(row('Free space',(p.disk_free_gb??'?')+' GB'));
    P.append(row('Photos',String(p.photos??'?')));
    if(j.sw)paintSw(P,j.sw);
  }
  const f=document.createElement('div');f.id='pf';
  const s=document.createElement('span');s.textContent=lastAt?'Updated '+new Date(lastAt).toTimeString().slice(0,8):'';
  const b=document.createElement('button');b.id='prf';b.textContent='Refresh';b.onclick=e=>{e.stopPropagation();info(true)};
  f.append(s,b);P.append(f);
}
// ---- software version + updates ----
const SLOT={current:'updated',previous:'previous',legacy:'built-in'};
let upBusy=false,upSeen=false;
function paintSw(P,w){
  P.append(document.createElement('hr'));
  const h=document.createElement('h4');h.textContent='Software';P.append(h);
  P.append(row('Version',(w.version||'?')+' · '+(SLOT[w.slot]||w.slot),w.slot==='current'?'':'warn'));
  if(w.update_available)P.append(row('Available',w.latest,'ok'));
  else if(w.checked_at)P.append(row('Checked',new Date(w.checked_at*1000).toLocaleString([],{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})+(w.check_error?' · failed':' · up to date'),w.check_error?'warn':''));
  if(w.check_error){const n=document.createElement('div');n.className='note';n.textContent=w.check_error;P.append(n)}
  if(w.update_available&&w.notes){const n=document.createElement('div');n.className='note';n.textContent=w.notes;P.append(n)}
  const a=document.createElement('div');a.className='ua';
  const btn=(t,fn,go)=>{const b=document.createElement('button');b.textContent=t;if(go)b.className='go';b.disabled=upBusy||w.busy;b.onclick=e=>{e.stopPropagation();fn()};a.append(b)};
  if(w.can_update){
    btn(upBusy?'Working…':'Check for update',upCheck);
    if(w.update_available)btn('Install '+w.latest,()=>upRun('/update/install',{},'Install version '+w.latest+'? The app restarts (about 20 s).'),true);
    if(w.previous&&w.previous!==w.version)btn('Back to '+w.previous,()=>upRun('/update/rollback',{to:'previous'},'Go back to version '+w.previous+'?'));
    else if(w.slot!=='legacy')btn('Use built-in '+(w.legacy||''),()=>upRun('/update/rollback',{to:'legacy'},'Start the built-in version '+(w.legacy||'')+'?'));
  }
  P.append(a);
}
async function upPost(url,body){
  upBusy=true;if(!$('pop').hidden)paintPop();
  try{
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
    const j=await r.json().catch(()=>({}));
    if(last&&j.sw)last.sw=j.sw;
    if(!r.ok)throw new Error(j.error||'HTTP '+r.status);
    return j;
  }finally{upBusy=false;if(!$('pop').hidden)paintPop()}
}
async function upCheck(){
  try{const j=await upPost('/update/check');toast(j.sw.update_available?'Version '+j.sw.latest+' is available':'Up to date ('+j.sw.version+')')}
  catch(e){toast('Update check failed: '+e.message,true)}
}
async function upRun(url,body,q){
  if(!confirm(q))return;
  const was=last&&last.sw?last.sw.version:null;
  try{await upPost(url,body)}catch(e){toast('Failed: '+e.message,true);return}
  toast('Restarting…',false,60000);upBusy=true;
  const t0=Date.now();
  await new Promise(r=>setTimeout(r,4000));
  while(Date.now()-t0<90000){
    try{const r=await fetch('/version',{cache:'no-store'});if(r.ok){const v=await r.json();toast('Running '+v.version+' ('+(SLOT[v.slot]||v.slot)+')',v.version===was&&v.slot!=='legacy',4000);setTimeout(()=>location.reload(),1500);return}}catch(e){}
    await new Promise(r=>setTimeout(r,2000));
  }
  upBusy=false;toast('No answer after restart - reload the page',true);
}
async function info(force){
  try{
    const r=await fetch('/status',{cache:'no-store'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    last=await r.json();lastAt=Date.now();
    if(!upSeen&&last.sw&&last.sw.update_available){upSeen=true;toast('Update '+last.sw.latest+' available - see device details',false,5000)}
  }catch(e){last=null}
  paintIcon(last?assess(last):null);
  if(!$('pop').hidden)paintPop();
}
let hideT=null;
function openPop(){
  clearTimeout(hideT);
  $('pop').hidden=false;paintPop();
  const r=$('sig').getBoundingClientRect(),pr=$('pop').getBoundingClientRect();
  $('pop').style.setProperty('--ax',Math.max(14,pr.right-(r.left+r.width/2)-6)+'px');
  $('sig').classList.add('on');
  if(!lastAt||Date.now()-lastAt>5000)info();
}
function closePop(){$('pop').hidden=true;$('sig').classList.remove('on')}
$('sig').onclick=e=>{e.stopPropagation();$('pop').hidden?openPop():closePop()};
if(matchMedia('(hover:hover) and (pointer:fine)').matches){
  const leave=()=>{clearTimeout(hideT);hideT=setTimeout(closePop,250)};
  $('sig').onmouseenter=openPop;$('sig').onmouseleave=leave;
  $('pop').onmouseenter=()=>clearTimeout(hideT);$('pop').onmouseleave=leave;
}
document.addEventListener('click',e=>{if(!$('pop').hidden&&!$('pop').contains(e.target))closePop();if(!$('sheet').hidden&&!$('sheet').contains(e.target)&&!$('ex').contains(e.target))$('sheet').hidden=true});
setInterval(()=>{if(!document.hidden)info()},60000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden){info();gal().catch(()=>{})}});

// ---- full-size viewer: download with progress, pinch / drag / double-tap zoom ----
async function openFull(){
  if(!cur)return;
  const name=cur;
  history.pushState({ov:1},'');
  $('ov').hidden=false;
  V.nw=0;
  $('ovimg').removeAttribute('src');
  $('ovsave').removeAttribute('href');
  $('ovp').value=0;$('ovtxt').textContent='connecting…';
  ctl=new AbortController();
  try{
    const r=await fetch('/f/'+name,{signal:ctl.signal});
    if(!r.ok)throw new Error('HTTP '+r.status);
    const total=+r.headers.get('Content-Length')||0,rd=r.body.getReader(),parts=[];
    let got=0;
    if(!total)$('ovp').removeAttribute('value');
    for(;;){
      const x=await rd.read();if(x.done)break;
      parts.push(x.value);got+=x.value.length;
      if(total)$('ovp').value=got/total;
      $('ovtxt').textContent=(got/1e6).toFixed(1)+(total?' / '+(total/1e6).toFixed(1):'')+' MB';
    }
    if(blobUrl)URL.revokeObjectURL(blobUrl);
    blobUrl=URL.createObjectURL(new Blob(parts,{type:'image/jpeg'}));
    $('ovimg').src=blobUrl;$('ovsave').href=blobUrl;$('ovsave').download=name;
    $('ovp').value=1;$('ovtxt').textContent=name+' · '+(got/1e6).toFixed(1)+' MB';
  }catch(e){if(e.name!=='AbortError')$('ovtxt').textContent='error: '+e.message}
}
function calcLimits(){
  const b=$('ovbox'),W=b.clientWidth,H=b.clientHeight,dpr=window.devicePixelRatio||1;
  V.min=Math.min(W/V.nw,H/V.nh);V.max=Math.max(V.min*4,2/dpr);
}
function applyV(anim){
  const b=$('ovbox'),W=b.clientWidth,H=b.clientHeight,iw=V.nw*V.s,ih=V.nh*V.s;
  V.x=iw<=W?(W-iw)/2:Math.min(0,Math.max(W-iw,V.x));
  V.y=ih<=H?(H-ih)/2:Math.min(0,Math.max(H-ih,V.y));
  const im=$('ovimg');
  im.style.transition=anim?'transform .22s ease-out':'none';
  im.style.transform='translate('+V.x+'px,'+V.y+'px) scale('+V.s+')';
}
function zoomAt(cx,cy,ns,anim){
  ns=Math.min(V.max,Math.max(V.min,ns));
  const k=ns/V.s;
  V.x=cx-(cx-V.x)*k;V.y=cy-(cy-V.y)*k;V.s=ns;applyV(anim);
}
$('ovimg').onload=()=>{
  const im=$('ovimg');V.nw=im.naturalWidth;V.nh=im.naturalHeight;
  im.style.width=V.nw+'px';im.style.height=V.nh+'px';
  calcLimits();V.s=V.min;V.x=V.y=0;applyV(false);
};
window.addEventListener('resize',()=>{
  if(!V.nw||$('ov').hidden)return;
  const atFit=V.s<=V.min*1.001;calcLimits();
  V.s=atFit?V.min:Math.min(V.max,Math.max(V.min,V.s));applyV(false);
});
const box=$('ovbox'),P=new Map();let moved=0,tap={t:0,x:0,y:0};
const bpt=e=>{const r=box.getBoundingClientRect();return[e.clientX-r.left,e.clientY-r.top]};
box.addEventListener('pointerdown',e=>{
  if(!V.nw)return;
  box.setPointerCapture(e.pointerId);
  if(P.size===0)moved=0;
  P.set(e.pointerId,bpt(e));
});
box.addEventListener('pointermove',e=>{
  if(!P.has(e.pointerId))return;
  const o=P.get(e.pointerId),c=bpt(e);
  if(P.size===1){
    V.x+=c[0]-o[0];V.y+=c[1]-o[1];moved+=Math.abs(c[0]-o[0])+Math.abs(c[1]-o[1]);applyV(false);
  }else if(P.size===2){
    const q2=[...P.entries()].find(([id])=>id!==e.pointerId)[1];
    const d0=Math.hypot(o[0]-q2[0],o[1]-q2[1]),d1=Math.hypot(c[0]-q2[0],c[1]-q2[1]);
    if(d0>0){
      const m0=[(o[0]+q2[0])/2,(o[1]+q2[1])/2],m1=[(c[0]+q2[0])/2,(c[1]+q2[1])/2];
      const ns=Math.min(V.max,Math.max(V.min,V.s*d1/d0)),k=ns/V.s;
      V.x=m1[0]-(m0[0]-V.x)*k;V.y=m1[1]-(m0[1]-V.y)*k;V.s=ns;moved+=10;applyV(false);
    }
  }
  P.set(e.pointerId,c);
});
function dbl(x,y){
  if(V.s>V.min*1.05)zoomAt(x,y,V.min,true);
  else zoomAt(x,y,Math.max(Math.min(V.max,1/(window.devicePixelRatio||1)),V.min*2),true);
}
function up(e){
  if(!P.has(e.pointerId))return;
  const single=P.size===1;P.delete(e.pointerId);
  if(single&&moved<8&&e.type==='pointerup'){
    const[x,y]=bpt(e),now=Date.now();
    if(now-tap.t<350&&Math.hypot(x-tap.x,y-tap.y)<30){tap.t=0;dbl(x,y)}else tap={t:now,x,y};
  }
}
box.addEventListener('pointerup',up);
box.addEventListener('pointercancel',up);
box.addEventListener('wheel',e=>{
  e.preventDefault();const[x,y]=bpt(e);
  zoomAt(x,y,V.s*Math.exp(-e.deltaY*0.0015),false);
},{passive:false});
$('zin').onclick=()=>zoomAt(box.clientWidth/2,box.clientHeight/2,V.s*1.6,true);
$('zout').onclick=()=>zoomAt(box.clientWidth/2,box.clientHeight/2,V.s/1.6,true);
$('ovclose').onclick=()=>history.back();
window.onpopstate=()=>{if(ctl)ctl.abort();$('ov').hidden=true};


// ---- stage: swipe / tap / keys ----
const stg=$('stage');let sw=null;
stg.addEventListener('pointerdown',e=>{if(e.target.closest('.nav'))return;sw={x:e.clientX,y:e.clientY,t:Date.now()}});
stg.addEventListener('pointerup',e=>{
  if(!sw)return;const dx=e.clientX-sw.x,dy=e.clientY-sw.y;sw=null;
  if(Math.abs(dx)>45&&Math.abs(dx)>Math.abs(dy)*1.5)step(dx<0?1:-1);
  else if(Math.abs(dx)<10&&Math.abs(dy)<10&&cur)openFull();
});
stg.addEventListener('pointercancel',()=>{sw=null});
$('prv').onclick=()=>step(-1);$('nxt').onclick=()=>step(1);
document.addEventListener('keydown',e=>{
  if(!$('ov').hidden||/INPUT|TEXTAREA/.test(document.activeElement.tagName))return;
  if(e.key==='ArrowLeft')step(-1);else if(e.key==='ArrowRight')step(1);
});
$('main').onload=()=>{$('main').classList.add('ok');$('stage').classList.remove('ld')};
$('main').onerror=()=>{if(cur){$('stage').classList.remove('ld');toast(cur+': preview missing or corrupt',true)}};

// ---- exposure sheet ----
function expo(){return{shutter:+$('sh').value||null,gain:+$('ga').value||null}}
function exdot(){const e=expo();$('exdot').hidden=!(e.shutter||e.gain)}
$('ex').onclick=e=>{e.stopPropagation();$('sheet').hidden=!$('sheet').hidden};
$('exdone').onclick=()=>{$('sheet').hidden=true};
$('exauto').onclick=()=>{$('sh').value='';$('ga').value='';exdot()};
$('sh').oninput=$('ga').oninput=exdot;

// ---- PHOTO ----
let busy=false;
$('go').onclick=async()=>{
  if(busy)return;busy=true;$('sheet').hidden=true;
  const g=$('go'),t0=Date.now();g.disabled=true;g.classList.add('busy');
  const tick=setInterval(()=>{$('golab').textContent='Capturing… '+Math.floor((Date.now()-t0)/1000)+' s'},250);
  $('golab').textContent='Capturing…';
  try{
    const r=await fetch('/shot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(expo())});
    const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||'HTTP '+r.status);
    await gal();show(j.name,true);
    window.scrollTo({top:0,behavior:'smooth'});
    toast('Photo saved');
  }catch(e){toast('Error: '+e.message,true)}
  clearInterval(tick);g.disabled=false;g.classList.remove('busy');$('golab').textContent='PHOTO';busy=false;
};

// ---- Delete (tap twice) ----
let armT=null;
function disarm(){clearTimeout(armT);$('del').classList.remove('arm');$('dellab').hidden=true}
$('del').onclick=async()=>{
  if(!cur)return;
  if(!$('del').classList.contains('arm')){
    $('del').classList.add('arm');$('dellab').hidden=false;armT=setTimeout(disarm,3000);return;
  }
  disarm();const n=cur,i=list.indexOf(n);
  $('del').disabled=true;
  try{
    const r=await fetch('/delete/'+n,{method:'POST'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    cur=null;
    await gal();
    if(list.length)show(list[Math.min(i,list.length-1)],false);
    toast('Deleted');
  }catch(e){toast('Error: '+e.message,true)}
  $('del').disabled=false;
};
info();gal().catch(e=>toast('Error: '+e.message,true));
</script></html>
"""


PAGE_LIVE = r"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Target Cam - Live</title>
<meta name=theme-color content="#0c2038">
<link rel=manifest href=/manifest.webmanifest>
<link rel=icon href=/icon-192.png type=image/png>
<link rel=apple-touch-icon href=/icon-apple.png>
<style>
:root{--bg:#0b111b;--hd:#0c2038;--card:#121c2b;--line:#243650;--btn:#16314f;--txt:#e8eef7;--mut:#7d8da3;--ok:#3ddc97;--acc:#2fbf84;--warn:#ffb020;--bad:#ff6b78}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding-bottom:calc(24px + env(safe-area-inset-bottom))}
button,a{font:inherit;color:inherit;border:0;cursor:pointer;text-decoration:none}
button:disabled{opacity:.6}
svg.i{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;display:block}
header{position:sticky;top:0;z-index:20;background:var(--hd);padding:calc(10px + env(safe-area-inset-top)) 14px 10px;display:flex;align-items:center;gap:10px}
header h1{flex:1;margin:0;font-size:18px;font-weight:650;letter-spacing:.2px}
.ic{width:40px;height:40px;border-radius:12px;background:#16314f;display:grid;place-items:center}
.wrap{max-width:960px;margin:0 auto;padding:0 12px}
#modes{display:flex;gap:10px;align-items:center;margin:12px 0 8px}
.seg{display:flex;flex:1;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:3px;gap:3px}
.seg button{flex:1;background:none;border-radius:11px;padding:.65em .5em;font-weight:600;color:var(--mut)}
.seg button.active{background:var(--btn);color:var(--txt);box-shadow:inset 0 0 0 1px #3a5f8a}
#bs{border-radius:14px;padding:.8em 1.3em;font-weight:700;background:var(--acc);color:#04271a}
#bs.danger{background:#b3263a;color:#fff}
#lst{font:12.5px ui-monospace,monospace;color:var(--mut);min-height:1.4em}
#view{width:100%;max-width:1280px;display:block;background:#000;border-radius:16px;margin-top:6px}
#hint{font-size:13px;color:#9ab;margin:8px 0;min-height:2.6em}
#nums{display:flex;justify-content:space-between;align-items:baseline;font:14px ui-monospace,monospace;margin-top:8px}
#pct{font:650 2.4rem system-ui,sans-serif}
#meter{height:22px;background:var(--card);border-radius:8px;overflow:hidden;margin:6px 0}
#bar{height:100%;width:0;background:#e66;transition:width .25s,background .25s}
#spark{width:100%;max-width:1280px;height:64px;display:block;background:var(--card);border-radius:12px}
#tools{display:flex;gap:10px;margin:12px 0}
#tools button{background:var(--btn);border-radius:12px;padding:.7em 1.1em;font-weight:600}
</style>
<header><a id=back class=ic href=/ aria-label=Back title=Back><svg class=i viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg></a><h1>Live view</h1></header>
<div class=wrap>
<div id=modes>
<div class=seg><button id=bw>WIDE</button><button id=bz>ZOOM 1:1</button></div>
<button id=bs class=danger>STOP</button>
</div>
<div id=lst>starting…</div>
<canvas id=view width=854 height=480></canvas>
<div id=hint></div>
<div id=nums><span id=pct>–</span><span id=nv></span></div>
<div id=meter><div id=bar></div></div>
<canvas id=spark width=600 height=64></canvas>
<div id=tools><button id=rp>RESET PEAK</button><button id=snd>SOUND: off</button></div>
</div>
<script>
const $=id=>document.getElementById(id);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let mode='wide',center=null,frac=[0.1853,0.1852],info=null,running=false,token=0,ctrl=null,last=null;
let hist=[],peak=0,skip=2,mb=0,times=[],au=null;

// ---- focus metric: 3x3 box blur -> Laplacian variance, normalised by mean^2 (independent of exposure) ----
function scoreLuma(g,w,h){
  let sum=0;for(let i=0;i<g.length;i++)sum+=g[i];
  const mean=sum/g.length,b=new Float32Array(g.length);
  for(let y=1;y<h-1;y++)for(let x=1;x<w-1;x++){const i=y*w+x;
    b[i]=(g[i-w-1]+g[i-w]+g[i-w+1]+g[i-1]+g[i]+g[i+1]+g[i+w-1]+g[i+w]+g[i+w+1])/9}
  let s=0,s2=0,n=0;
  for(let y=2;y<h-2;y++)for(let x=2;x<w-2;x++){const i=y*w+x,l=4*b[i]-b[i-1]-b[i+1]-b[i-w]-b[i+w];s+=l;s2+=l*l;n++}
  const m=s/n;return 10000*(s2/n-m*m)/(mean*mean+1);
}
const mc=document.createElement('canvas'),mx=mc.getContext('2d',{willReadFrequently:true});
function focusScore(bmp){      // measured area: the central 60% of the frame (the thin frame drawn on the image)
  const rw=Math.round(bmp.width*.6),rh=Math.round(bmp.height*.6),rx=(bmp.width-rw)>>1,ry=(bmp.height-rh)>>1;
  mc.width=rw;mc.height=rh;mx.drawImage(bmp,rx,ry,rw,rh,0,0,rw,rh);
  const d=mx.getImageData(0,0,rw,rh).data,g=new Float32Array(rw*rh);
  for(let i=0,j=0;j<g.length;i+=4,j++)g[j]=.299*d[i]+.587*d[i+1]+.114*d[i+2];
  return scoreLuma(g,rw,rh);
}

// ---- drawing ----
function clampC(c){return{x:Math.min(Math.max(c.x,frac[0]/2),1-frac[0]/2),y:Math.min(Math.max(c.y,frac[1]/2),1-frac[1]/2)}}
function draw(){
  if(!last)return;
  const c=$('view'),x=c.getContext('2d'),w=c.width,h=c.height;
  x.drawImage(last,0,0);
  x.setLineDash([]);x.lineWidth=1;x.strokeStyle='rgba(255,255,255,.55)';
  x.strokeRect(w*.2,h*.2,w*.6,h*.6);
  if(mode==='wide'&&center){
    const k=clampC(center);x.setLineDash([8,5]);x.lineWidth=2;x.strokeStyle='#fc3';
    x.strokeRect((k.x-frac[0]/2)*w,(k.y-frac[1]/2)*h,frac[0]*w,frac[1]*h);
  }
}
function spark(){
  const c=$('spark'),x=c.getContext('2d'),w=c.width,h=c.height;
  x.clearRect(0,0,w,h);x.strokeStyle='#333';x.lineWidth=1;x.beginPath();x.moveTo(0,h*.05);x.lineTo(w,h*.05);x.stroke();
  if(!hist.length||peak<=0)return;
  x.strokeStyle='#4d8';x.lineWidth=2;x.beginPath();
  hist.forEach((v,i)=>{const px=hist.length>1?i*(w-6)/(60-1):0,py=h-4-(v/peak)*(h-10);i?x.lineTo(px,py):x.moveTo(px,py)});
  x.stroke();
}
function record(s){
  if(skip>0){skip--;return}
  hist.push(s);if(hist.length>60)hist.shift();
  if(s>peak)peak=s;
  const rel=peak>0?s/peak:0,col=rel>=.95?'#4d8':rel>=.8?'#fc3':'#e66';
  $('pct').textContent=Math.round(rel*100)+'%';$('pct').style.color=col;
  $('bar').style.width=(rel*100)+'%';$('bar').style.background=col;
  $('nv').textContent='now '+Math.round(s)+' · peak '+Math.round(peak);
  spark();
  if(au)au.o.frequency.setTargetAtTime(200+1000*rel*rel,au.c.currentTime,.08);
}
function resetPeak(){peak=0;hist=[];skip=2;$('pct').textContent='–';$('nv').textContent='';$('bar').style.width='0';spark()}
function soundOff(){if(au){try{au.o.stop();au.c.close()}catch(e){}au=null}$('snd').textContent='SOUND: off'}

// ---- UI state ----
function modeLabel(){
  if(mode==='wide')return 'WIDE';
  return info&&info.fallback?'ZOOM 2:1 (binned)':'ZOOM 1:1';
}
function buttons(){
  $('bw').classList.toggle('active',running&&mode==='wide');
  $('bz').classList.toggle('active',running&&mode==='zoom');
  $('bs').textContent=running?'STOP':'START';
  $('bs').className=running?'danger':'';
}
function hint(){
  $('hint').textContent=mode==='wide'
    ?'WIDE: tap the target to place the zoom window (yellow), then press ZOOM 1:1.'
    :(info&&info.fallback
      ?'Native 4608 px mode unavailable - using binned 2:1 zoom (less fine). Turn the lens ring slowly; keep the % near 100.'
      :'Native pixels. Turn the lens ring slowly (the view lags 1-2 s) and keep the % near 100; RESET PEAK after moving the camera.');
}
function halt(msg){token++;running=false;if(ctrl)ctrl.abort();soundOff();$('lst').textContent=msg;buttons()}

async function pump(my){
  let seq=0;
  while(my===token){
    try{
      const r=await fetch('/live/frame?after='+seq,{cache:'no-store',signal:ctrl.signal});
      if(my!==token)return;
      if(r.status===204)continue;
      if(r.status===409){const j=await r.json().catch(()=>({}));halt(j.error||'stopped');return}
      if(!r.ok)throw new Error('HTTP '+r.status);
      seq=+r.headers.get('X-Seq');mb=(+r.headers.get('X-Bytes')||0)/1e6;
      const bmp=await createImageBitmap(await r.blob());
      if(my!==token)return;
      last=bmp;const c=$('view');if(c.width!==bmp.width||c.height!==bmp.height){c.width=bmp.width;c.height=bmp.height}
      draw();record(focusScore(bmp));
      times.push(performance.now());if(times.length>6)times.shift();
      const fps=times.length>1?(times.length-1)*1000/(times[times.length-1]-times[0]):0;
      $('lst').textContent=modeLabel()+' · '+fps.toFixed(1)+' fps · '+mb.toFixed(1)+' MB';
    }catch(e){
      if(my!==token)return;
      $('lst').textContent='link error: '+e.message;await sleep(1500);
    }
  }
}
async function start(m){
  halt('starting…');
  mode=m;running=true;buttons();hint();
  const my=++token;ctrl=new AbortController();
  $('lst').textContent=(m==='zoom'?'starting zoom (a few s)…':'starting…');
  try{
    const c=center?clampC(center):{x:.5,y:.5};
    const r=await fetch('/live/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode:m,cx:c.x,cy:c.y}),signal:ctrl.signal});
    const j=await r.json();
    if(my!==token)return;
    if(!r.ok)throw new Error(j.error||('HTTP '+r.status));
    info=j;if(m==='zoom'&&j.frac)frac=j.frac;
    resetPeak();times=[];hint();
    pump(my);
  }catch(e){
    if(my!==token)return;
    running=false;buttons();$('lst').textContent='error: '+e.message;
  }
}
async function stop(){
  halt('stopped');
  try{await fetch('/live/stop',{method:'POST',keepalive:true})}catch(e){}
}

$('bw').onclick=()=>start('wide');
$('bz').onclick=()=>start('zoom');
$('bs').onclick=()=>running?stop():start(mode);
$('rp').onclick=resetPeak;
$('view').addEventListener('click',e=>{
  if(mode!=='wide')return;
  const r=$('view').getBoundingClientRect();
  center=clampC({x:(e.clientX-r.left)/r.width,y:(e.clientY-r.top)/r.height});draw();
});
$('snd').onclick=()=>{
  if(au){soundOff();return}
  const C=window.AudioContext||window.webkitAudioContext,c=new C(),o=c.createOscillator(),g=c.createGain();
  g.gain.value=.04;o.type='sine';o.connect(g);g.connect(c.destination);o.start();
  au={c,o};$('snd').textContent='SOUND: on';
};
// leaving or hiding the page must release the camera (and stop the LTE traffic)
function release(){if(running){navigator.sendBeacon('/live/stop');halt('paused - press START')}}
addEventListener('pagehide',release);
document.addEventListener('visibilitychange',()=>{if(document.hidden)release()});
start('wide');
</script></html>"""


@app.get("/live")
def live_page():
    return no_store(app.response_class(PAGE_LIVE, mimetype="text/html"))


@app.get("/")
def index():
    return no_store(app.response_class(PAGE, mimetype="text/html"))


cleanup_startup()

if __name__ == "__main__":
    threading.Thread(target=background_update_tasks, daemon=True).start()
    app.run(host=HOST, port=PORT, threaded=True)
