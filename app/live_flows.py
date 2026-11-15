#!/usr/bin/env python3
"""AIOStreams Live Flows: polls AIOStreams' dashboard feed every 2 s and serves a live page. Standard library only.

AIOStreams has no read-only role, so the login this uses could also stop streams or ban users. The only POST this program
ever sends is the login itself: everything else goes through aio_get, which refuses any path outside READS.

TorBox (optional, TORBOX_API_KEY): a separate thread reads TorBox's public status page and, with the key, only GETs the
account and the torrent list (TORBOX_READS) once a minute. The key stays on the server; no torrent names reach the page.

History and events are saved to FLOWS_DATA every 30 s and on shutdown. Stream URLs never reach the browser or the disk, only a source label.
"""
import base64
import collections
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import signal
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TESTED_VERSION = "2.37.0"
STEP = 2
POINTS = 450
SAVE_EVERY = 30
ICONS = ("/icons/icon-180.png", "/icons/icon-192.png", "/icons/icon-512.png")
LOGIN_RETRY = 300
LOGIN_RETRY_UNREACHABLE = 15   # AIOStreams not listening yet, e.g. both just started
READS = (
    "/dashboard/streams/live",
    "/dashboard/streams/history?limit=8",
    "/dashboard/streams/bandwidth?window=30d",
    "/dashboard/analytics/addons?range=24h",
    "/dashboard/system",
)


def env_on(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def parse_names(raw):
    """FLOWS_SOURCE_NAMES: debrid=TorBox,addon.example.org=My addon"""
    return {k.strip().lower(): v.strip() for k, v in (p.split("=", 1) for p in raw.split(",") if "=" in p)}


AIO_URL = os.environ.get("AIOSTREAMS_URL", "").strip().rstrip("/")
AIO_USER = os.environ.get("AIOSTREAMS_USER", "")
AIO_PASSWORD = os.environ.get("AIOSTREAMS_PASSWORD", "")
DEMO = env_on("FLOWS_DEMO")
HIDE = env_on("FLOWS_HIDE_VIEWERS")
PAGE_PASSWORD = os.environ.get("FLOWS_PASSWORD", "")
SOURCE_NAMES = parse_names(os.environ.get("FLOWS_SOURCE_NAMES", ""))
BIND = os.environ.get("FLOWS_BIND", "0.0.0.0")
PORT = int(os.environ.get("FLOWS_PORT", "8300"))
DATA_FILE = os.path.join(os.environ.get("FLOWS_DATA", "/data"), "state.json")
TORBOX_KEY = os.environ.get("TORBOX_API_KEY", "").strip()
# Parts of TorBox's status page that streaming from TorBox depends on.
TORBOX_PARTS = [p.strip() for p in os.environ.get("TORBOX_STATUS_PARTS", "API,Database,Public torrents,Private torrents").split(",") if p.strip()]
TORBOX_API = "https://api.torbox.app/v1/api"
TORBOX_STATUS = "https://status.torbox.app/index.json"
TORBOX_READS = ("/user/me", "/torrents/mylist")
TORBOX_EVERY = 60
TORBOX_PLANS = {0: "Free", 1: "Essential", 2: "Pro", 3: "Standard"}

history = collections.deque(maxlen=POINTS)
events = collections.deque(maxlen=40)
state = {"ready": False}
lock = threading.Lock()
demo = None
_cache = {}
_geo = {}
_seen = {"viewers": {}, "feed_ok": None}
_aio = {"cookie": None, "retry": 0, "error": None}
_save_failed = {"said": False}


def save():
    """Written beside the real file and swapped in, so a crash can't leave half a file. Holds viewer IPs and filenames, so owner only."""
    if DEMO:
        return
    try:
        tmp = DATA_FILE + ".tmp"
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"hidden": HIDE, "history": list(history), "events": list(events), "viewers": _seen["viewers"]}, f)
        os.replace(tmp, DATA_FILE)
        _save_failed["said"] = False
    except (OSError, RuntimeError, ValueError) as e:
        # RuntimeError: the poller changed a list while the shutdown handler was copying it.
        if not _save_failed["said"]:
            print(f"Keeping data in memory only, can't write to {DATA_FILE}: {type(e).__name__}", flush=True)
        _save_failed["said"] = True


def load():
    try:
        with open(DATA_FILE) as f:
            saved = json.load(f)
        now = int(time.time() * 1000)
        points = [[int(p[0]), p[1]] for p in saved["history"] if now - p[0] <= POINTS * STEP * 1000]
        # Saved before FLOWS_HIDE_VIEWERS was turned on: those events and viewers must not come back.
        keep = saved.get("hidden") or not HIDE
        saved_events = list(saved["events"]) if keep else []
        viewers = dict(saved["viewers"]) if keep else {}
        if not all(isinstance(e, dict) for e in saved_events):
            raise ValueError("events")
    except FileNotFoundError:
        return
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as e:
        print(f"Starting without saved data: {type(e).__name__}", flush=True)
        return
    history.extend(points)
    if points:
        # A gap in the line for the time this wasn't running, rather than a straight line across it.
        history.append([now, None])
    events.extend(saved_events)
    _seen["viewers"] = viewers


def stop(*_args):
    save()
    sys.exit(0)


def event(level, text):
    events.appendleft({"t": int(time.time() * 1000), "level": level, "text": text})


def cached(key, ttl, fn):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could carry the session cookie somewhere else."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(NoRedirect)


def aio_call(path, data=None, cookie=None):
    headers = {"Accept": "application/json", "User-Agent": "aiostreams-live-flows"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(AIO_URL + "/api/v1" + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers=headers, method="POST" if data is not None else "GET")
    try:
        with _opener.open(req, timeout=8) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def aio_login():
    """One attempt, then a pause, so a wrong password can't trip AIOStreams' login rate limit."""
    if time.monotonic() < _aio["retry"]:
        return False
    _aio["retry"] = time.monotonic() + LOGIN_RETRY
    try:
        status, _body, headers = aio_call("/auth/login", {"username": AIO_USER, "password": AIO_PASSWORD})
    except OSError:
        # No answer is not a refused password: try again soon, it can't trip the rate limit.
        _aio.update(retry=time.monotonic() + LOGIN_RETRY_UNREACHABLE, error="Can't reach AIOStreams at AIOSTREAMS_URL")
        return False
    for c in headers.get_all("Set-Cookie") or []:
        if status == 200 and c.startswith("aiostreams.session="):
            _aio.update(cookie=c.split(";", 1)[0], error=None, retry=0)
            return True
    _aio["error"] = {
        401: "AIOStreams refused the login. Check AIOSTREAMS_USER and AIOSTREAMS_PASSWORD",
        403: "Password login is turned off in AIOStreams",
        429: "AIOStreams is rate limiting logins",
    }.get(status, f"AIOStreams login failed with status {status}")
    return False


class TorBoxRefused(Exception):
    pass


def torbox_get(path):
    """GET one TorBox API path with the key. The only way this program uses the key: it never sends anything else to TorBox."""
    if path not in TORBOX_READS:
        raise ValueError("not an allowed TorBox read")
    req = urllib.request.Request(TORBOX_API + path, headers={"Authorization": "Bearer " + TORBOX_KEY, "Accept": "application/json",
                                                             "User-Agent": "aiostreams-live-flows"})
    try:
        with _opener.open(req, timeout=10) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise TorBoxRefused() from None
        raise ValueError(f"status {e.code}") from None
    if not isinstance(body, dict) or not body.get("success", True):
        raise ValueError("TorBox said no")
    return body.get("data")


def torbox_status_page():
    """TorBox's public status page (no key): the watched parts that aren't operational, and open notices about them."""
    req = urllib.request.Request(TORBOX_STATUS, headers={"Accept": "application/json", "User-Agent": "aiostreams-live-flows"})
    with _opener.open(req, timeout=10) as r:
        included = json.load(r).get("included") or []
    parts = {i.get("id"): (i.get("attributes") or {}).get("public_name") for i in included if i.get("type") == "status_page_resource"}
    issues = [{"name": a.get("public_name"), "status": a.get("status")} for a in
              ((i.get("attributes") or {}) for i in included if i.get("type") == "status_page_resource")
              if a.get("public_name") in TORBOX_PARTS and a.get("status") != "operational"]
    reports = []
    for i in included:
        a = i.get("attributes") or {}
        if i.get("type") != "status_report" or a.get("aggregate_state") in (None, "resolved", "operational"):
            continue
        if any(parts.get(str(x.get("status_page_resource_id"))) in TORBOX_PARTS for x in a.get("affected_resources") or []):
            reports.append({"title": str(a.get("title") or "")[:120], "ends": a.get("ends_at")})
    return {"issues": issues, "reports": reports[:3]}


def torrent_counts(torrents):
    """Counts only: no names or hashes leave the server."""
    out = {"total": 0, "downloading": 0, "ready": 0, "inactive": 0, "failed": 0}
    for t in torrents if isinstance(torrents, list) else []:
        if not isinstance(t, dict):
            continue
        st = str(t.get("download_state") or "").lower()
        out["total"] += 1
        if "error" in st or "failed" in st:
            out["failed"] += 1
        elif st == "expired" or "stalled" in st or "missing" in st:
            out["inactive"] += 1
        elif not t.get("download_finished"):
            out["downloading"] += 1
        else:
            out["ready"] += 1
    return out


def torbox_read():
    out = {"label": SOURCE_NAMES.get("debrid", "Debrid"), "checked": int(time.time() * 1000), "status": None,
           "account": None, "torrents": None, "error": None}
    try:
        out["status"] = demo.torbox_status() if demo else torbox_status_page()
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    try:
        me = (demo.torbox("/user/me") if demo else torbox_get("/user/me")) or {}
        out["account"] = {"plan": TORBOX_PLANS.get(me.get("plan")), "expires": me.get("premium_expires_at"), "renews": me.get("is_subscribed") is True}
        out["torrents"] = torrent_counts(demo.torbox("/torrents/mylist") if demo else torbox_get("/torrents/mylist"))
    except TorBoxRefused:
        out["error"] = "TorBox refused the API key. Check TORBOX_API_KEY"
    except (OSError, ValueError, AttributeError, TypeError):
        out["error"] = "Can't reach TorBox's API"
    return out


def torbox_problem(t):
    if not t:
        return None
    if t["error"]:
        return t["error"]
    issues = (t["status"] or {}).get("issues") or []
    return "TorBox: " + ", ".join(f"{i['name']} {i['status']}" for i in issues) if issues else None


def torbox_loop():
    while True:
        value = torbox_read()
        with lock:
            before = torbox_problem(state.get("torbox")) if state.get("torbox") else ""
        now = torbox_problem(value)
        if before != "" and now != before:
            event("warn" if now else "good", now or "TorBox is back to normal")
        with lock:
            state["torbox"] = value
        time.sleep(TORBOX_EVERY)


def unsupported():
    return f"AIOStreams {read_version() or 'on this server'} answered in an unexpected way. Live Flows was tested with {TESTED_VERSION}"


def aio_get(path):
    """GET one dashboard path. Returns (data, error). This is the only way this program reads AIOStreams' dashboard."""
    if path not in READS:
        raise ValueError("not an allowed read")
    if DEMO:
        return demo.read(path), None
    try:
        for _ in range(2):
            if not _aio["cookie"] and not aio_login():
                return None, _aio["error"] or "Waiting to retry the login"
            status, body, _h = aio_call(path, cookie=_aio["cookie"])
            if status == 200:
                data = json.loads(body).get("data")
                if not isinstance(data, dict):
                    return None, unsupported()
                return data, None
            if status == 403:
                _aio.update(cookie=None, retry=time.monotonic() + LOGIN_RETRY, error="This AIOStreams user is not an admin")
                return None, _aio["error"]
            if status == 404:
                return None, unsupported()
            if status != 401:
                return None, f"AIOStreams answered with status {status}"
            # The session ran out: log in once more.
            _aio["cookie"] = None
        return None, _aio["error"] or "AIOStreams keeps refusing the session"
    except ValueError:
        return None, unsupported()
    except OSError:
        return None, "Can't reach AIOStreams at AIOSTREAMS_URL"


def read_version():
    def read():
        if DEMO:
            return demo.VERSION
        try:
            status, body, _h = aio_call("/status")
            return (json.loads(body).get("data") or {}).get("version") if status == 200 else None
        except (OSError, ValueError, AttributeError):
            return None
    return cached("version", 300, read)


def source_of(transport, url):
    """Only this label leaves the program, never the URL. AIOStreams doesn't say which debrid service a stream uses."""
    url = url or ""
    if transport == "usenet":
        key = "usenet"
    elif "/debrid/playback/" in url:
        key = "debrid"
    else:
        try:
            key = (urllib.parse.urlsplit(url).hostname or "other").lower()
        except ValueError:
            key = "other"
    return SOURCE_NAMES.get(key) or {"usenet": "Usenet", "debrid": "Debrid", "other": "Other"}.get(key, key)


def session_key(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:10]


def viewer_fields(s):
    if HIDE:
        return {"ip": None, "country": None, "user": None, "file": None}
    return {"ip": s.get("clientIp"), "country": country(s.get("clientIp")), "user": s.get("username"), "file": s.get("filename")}


def viewers():
    data, error = aio_get("/dashboard/streams/live")
    if data is None:
        return {"ok": False, "error": error}
    if not isinstance(data.get("streams"), list):
        return {"ok": False, "error": unsupported()}
    streams = []
    for s in data["streams"]:
        streams.append({
            "key": session_key(s.get("id", "")),
            "source": source_of(s.get("transport"), s.get("displayUrl")),
            "size": s.get("size"),
            "pos": (s.get("start") or 0) + (s.get("currentBytes") or 0),
            "served": s.get("bytesServed"),
            "requests": s.get("requests"),
            "started": s.get("startedAt"),
            "activity": s.get("activity"),
            "bps": s.get("bytesPerSec"),
            **viewer_fields(s),
        })
    return {"ok": True, "streams": streams, "total_bps": (data.get("summary") or {}).get("totalBytesPerSec", 0)}


def stats():
    def addons():
        data, _e = aio_get("/dashboard/analytics/addons?range=24h")
        if data is None:
            return None
        return [{"name": a.get("presetId"), "requests": a.get("requests"), "errors": a.get("errors"),
                 "error_rate": a.get("errorRate"), "latency_ms": a.get("avgLatencyMs")} for a in data.get("addons", [])]

    def bandwidth():
        data, _e = aio_get("/dashboard/streams/bandwidth?window=30d")
        if data is None:
            return None
        return {"total": data.get("total"), "days": [[b.get("bucketMs"), b.get("bytes")] for b in data.get("series", [])]}

    def finished():
        data, _e = aio_get("/dashboard/streams/history?limit=8")
        if data is None:
            return None
        return [{"key": session_key(e.get("id", "")), "source": source_of(e.get("transport"), e.get("displayUrl")), "size": e.get("size"),
                 "served": e.get("bytesServed"), "requests": e.get("requests"), "started": e.get("startedAt"), "ended": e.get("endedAt"),
                 "reason": e.get("endReason"), **viewer_fields(e)} for e in data.get("entries", [])]

    def system():
        data, _e = aio_get("/dashboard/system")
        if data is None:
            return None
        cpu, mem, disk = data.get("cpu") or {}, data.get("memory") or {}, data.get("disk") or {}
        return {"cpu_pct": cpu.get("total"), "load1": (cpu.get("loadavg") or [None])[0], "cores": cpu.get("cores"),
                "mem_used": mem.get("used"), "mem_total": mem.get("total"), "disk_used": disk.get("used"), "disk_total": disk.get("total")}

    return {"addons": cached("addons", 60, addons), "bandwidth": cached("bandwidth", 300, bandwidth),
            "history": cached("history", 30, finished), "system": cached("system", 10, system)}


def country(ip):
    """Offline lookup: a binary search through the sorted DB-IP file, so it needs little memory and sends the IP nowhere."""
    if ip in _geo:
        return _geo[ip]
    code = None
    try:
        addr = int(ipaddress.ip_address(ip))
        path = os.path.join(HERE, "geo", "v6.csv" if ":" in ip else "v4.csv")
        with open(path, "rb") as f:
            lo, hi = 0, os.fstat(f.fileno()).st_size
            while hi - lo > 1:
                mid = (lo + hi) // 2
                f.seek(mid)
                f.readline()
                line = f.readline()
                if line and int(ipaddress.ip_address(line.decode().split(",")[0])) <= addr:
                    lo = mid
                else:
                    hi = mid
            f.seek(lo)
            if lo:
                f.readline()
            for raw in f:
                start, end, cc = raw.decode().strip().split(",")
                if int(ipaddress.ip_address(start)) > addr:
                    break
                if addr <= int(ipaddress.ip_address(end)):
                    code = None if cc == "ZZ" else cc.lower()
                    break
    except (OSError, ValueError, TypeError):
        pass
    if len(_geo) > 2000:
        _geo.clear()
    _geo[ip] = code
    return code


def viewer_events(feed):
    current = {s["key"]: s for s in feed["streams"]}
    known = _seen["viewers"]
    for key, s in current.items():
        if key not in known:
            event("good", f"{s.get('ip') or 'Viewer'} started {s.get('file') or 'a stream'}")
        elif (s.get("requests") or 0) > (known[key].get("requests") or 0):
            event("info", f"{s.get('ip') or 'Viewer'} made a new request (seek or reconnect)")
    for key, s in known.items():
        if key not in current:
            event("info", f"{s.get('ip') or 'Viewer'} finished {s.get('file') or 'a stream'}")
    _seen["viewers"] = current


def poll():
    feed = viewers()
    if _seen["feed_ok"] is not None and _seen["feed_ok"] != feed["ok"]:
        event("good" if feed["ok"] else "crit", "AIOStreams feed is back" if feed["ok"] else "AIOStreams feed stopped answering")
    if _seen["feed_ok"] != feed["ok"]:
        print("AIOStreams feed OK" if feed["ok"] else f"AIOStreams feed problem: {feed['error']}", flush=True)
    _seen["feed_ok"] = feed["ok"]
    if feed["ok"]:
        viewer_events(feed)
    to_viewers = feed.get("total_bps", 0) * 8 / 1e6 if feed["ok"] else None
    history.append([int(time.time() * 1000), to_viewers])
    with lock:
        state.update({
            "ready": True,
            "updated": int(time.time() * 1000),
            "demo": DEMO,
            "hidden": HIDE,
            "version": read_version(),
            "to_viewers": to_viewers,
            "viewers": feed,
            "stats": stats(),
            "history": list(history),
            "events": list(events)[:15],
        })


def poller():
    saved_at = time.monotonic()
    while True:
        started = time.monotonic()
        try:
            poll()
        except Exception as e:
            event("crit", f"Collector error: {type(e).__name__}")
        if started - saved_at >= SAVE_EVERY:
            save()
            saved_at = started
        time.sleep(max(0.2, STEP - (time.monotonic() - started)))


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "live-flows"
    sys_version = ""

    def send(self, code, kind, body, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        if not PAGE_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            given = base64.b64decode(header[6:], validate=True).partition(b":")[2]
        except ValueError:
            return False
        return hmac.compare_digest(given, PAGE_PASSWORD.encode())

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self.send(200, "text/plain", b"ok")
        # Browsers fetch these without the page's password, and they hold nothing private.
        elif path == "/manifest.webmanifest":
            with open(os.path.join(HERE, "manifest.webmanifest"), "rb") as f:
                self.send(200, "application/manifest+json", f.read())
        elif path in ICONS:
            with open(os.path.join(HERE, path.lstrip("/")), "rb") as f:
                self.send(200, "image/png", f.read())
        elif not self.allowed():
            self.send(401, "text/plain", b"password required", [("WWW-Authenticate", 'Basic realm="AIOStreams Live Flows", charset="UTF-8"')])
        elif path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send(200, "text/html; charset=utf-8", f.read())
        elif path == "/api/state":
            with lock:
                body = json.dumps(state).encode()
            self.send(200, "application/json", body)
        elif re.fullmatch(r"/flags/[a-z]{2}\.svg", path):
            try:
                with open(os.path.join(HERE, path.lstrip("/")), "rb") as f:
                    self.send(200, "image/svg+xml", f.read())
            except OSError:
                self.send(404, "text/plain", b"not found")
        else:
            self.send(404, "text/plain", b"not found")

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def config_problem():
    if DEMO:
        return None
    missing = [n for n, v in (("AIOSTREAMS_URL", AIO_URL), ("AIOSTREAMS_USER", AIO_USER), ("AIOSTREAMS_PASSWORD", AIO_PASSWORD)) if not v]
    if missing:
        return "Missing " + ", ".join(missing) + ". Set them, or set FLOWS_DEMO=1 to try the page with simulated data."
    return None


def start_demo():
    global demo
    import demo as demo_module
    demo = demo_module
    _geo.update(demo.COUNTRIES)
    for t, mbit in demo.backfill(POINTS, STEP):
        history.append([t, mbit])


if __name__ == "__main__":
    problem = config_problem()
    if problem:
        sys.exit(problem)
    if DEMO:
        start_demo()
    else:
        load()
    signal.signal(signal.SIGTERM, stop)
    print(f"AIOStreams Live Flows on {BIND}:{PORT}" + (" (demo data)" if DEMO else ""), flush=True)
    threading.Thread(target=poller, daemon=True).start()
    if TORBOX_KEY or DEMO:
        threading.Thread(target=torbox_loop, daemon=True).start()
    Server((BIND, PORT), Handler).serve_forever()
