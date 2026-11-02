#!/usr/bin/env python3
"""AIOStreams Live Flows: polls AIOStreams' dashboard feed every 2 s and serves a live page. Standard library only.

AIOStreams has no read-only role, so the login this uses could also stop streams or ban users. The only POST this program
ever sends is the login itself: everything else goes through aio_get, which refuses any path outside READS.

History and events live in memory and are gone after a restart. Stream URLs never reach the browser, only a source label.
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
LOGIN_RETRY = 300
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

history = collections.deque(maxlen=POINTS)
events = collections.deque(maxlen=40)
state = {"ready": False}
lock = threading.Lock()
demo = None
_cache = {}
_geo = {}
_seen = {"viewers": {}, "feed_ok": None}
_aio = {"cookie": None, "retry": 0, "error": None}


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
    status, _body, headers = aio_call("/auth/login", {"username": AIO_USER, "password": AIO_PASSWORD})
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
    while True:
        started = time.monotonic()
        try:
            poll()
        except Exception as e:
            event("crit", f"Collector error: {type(e).__name__}")
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
    print(f"AIOStreams Live Flows on {BIND}:{PORT}" + (" (demo data)" if DEMO else ""), flush=True)
    threading.Thread(target=poller, daemon=True).start()
    Server((BIND, PORT), Handler).serve_forever()
