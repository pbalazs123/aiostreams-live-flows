"""Runs the real code against a fake AIOStreams. Run with: python -m unittest discover tests"""
import base64
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import live_flows as lf  # noqa: E402

SECRET_URL = "https://stream.example.net/stream/SECRET-TOKEN/file.mkv"
LIVE = {"streams": [
    {"id": "s1", "transport": "proxy", "username": "friend", "clientIp": "203.0.113.5", "targetKey": "SECRET-KEY", "filename": "Film.One.mkv",
     "displayUrl": SECRET_URL, "size": 100, "bytesServed": 10, "requests": 1, "startedAt": 1, "activity": "streaming", "start": 5, "currentBytes": 5, "bytesPerSec": 1000000},
    {"id": "s2", "transport": "proxy", "username": "pal", "clientIp": "198.51.100.9", "targetKey": "SECRET-KEY", "filename": "Film.Two.mkv",
     "displayUrl": "https://aio.example.net/debrid/playback/SECRET-TOKEN", "size": 100, "bytesServed": 10, "requests": 1, "startedAt": 1, "activity": "paused", "bytesPerSec": 0},
    {"id": "s3", "transport": "usenet", "username": "", "clientIp": "198.51.100.10", "targetKey": "SECRET-KEY", "filename": "Film.Three.mkv", "size": 0, "bytesPerSec": 0},
], "summary": {"totalBytesPerSec": 1000000}}
ANSWERS = {
    "/api/v1/dashboard/streams/live": LIVE,
    "/api/v1/dashboard/streams/history?limit=8": {"entries": [
        {"id": "h1", "transport": "proxy", "username": "friend", "clientIp": "203.0.113.5", "targetKey": "SECRET-KEY", "filename": "Old.Film.mkv",
         "displayUrl": SECRET_URL, "bytesServed": 5, "startedAt": 1, "endedAt": 2, "endReason": "idle"}]},
    "/api/v1/dashboard/streams/bandwidth?window=30d": {"total": 2**30, "series": [{"bucketMs": 1, "bytes": 2**30}]},
    "/api/v1/dashboard/analytics/addons?range=24h": {"addons": [{"presetId": "comet", "requests": 9, "errors": 1, "errorRate": 11.1, "avgLatencyMs": 100}]},
    "/api/v1/dashboard/system": {"cpu": {"cores": 4, "total": 12, "loadavg": [0.5, 0, 0]}, "memory": {"total": 8, "used": 5, "rss": 2}, "disk": {"path": "/SECRET-PATH", "total": 9, "used": 3},
                                 "history": [1], "process": {"pid": 1}},
}


class FakeAio(http.server.BaseHTTPRequestHandler):
    seen = []
    login_status = 200
    feed_status = 200
    feed_body = None
    expire_next = False

    def reply(self, status, payload, cookie=None, location=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        cls = type(self)
        length = int(self.headers.get("Content-Length", 0))
        creds = json.loads(self.rfile.read(length))
        cls.seen.append(("POST", self.path, None))
        ok = cls.login_status == 200 and creds == {"username": "watcher", "password": "pw"}
        self.reply(200 if ok else (cls.login_status if cls.login_status != 200 else 401), {"data": {}},
                   "aiostreams.session=abc; Path=/; HttpOnly" if ok else None)

    def do_GET(self):
        cls = type(self)
        cls.seen.append(("GET", self.path, self.headers.get("Cookie")))
        if self.path == "/api/v1/status":
            self.reply(200, {"data": {"version": "9.9.9", "users": 3, "settings": {"x": "SECRET-SETTING"}}})
        elif cls.expire_next:
            cls.expire_next = False
            self.reply(401, {"data": None})
        elif cls.feed_status != 200:
            self.reply(cls.feed_status, {"data": None})
        elif cls.feed_body is not None:
            self.reply(200, cls.feed_body)
        elif self.headers.get("Cookie") != "aiostreams.session=abc":
            self.reply(401, {"data": None})
        else:
            self.reply(200, {"data": ANSWERS[self.path]})

    def log_message(self, *args):
        pass


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aio = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAio)
        threading.Thread(target=cls.aio.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.aio.shutdown()
        cls.aio.server_close()

    def setUp(self):
        FakeAio.seen, FakeAio.login_status, FakeAio.feed_status, FakeAio.feed_body, FakeAio.expire_next = [], 200, 200, None, False
        lf.AIO_URL, lf.AIO_USER, lf.AIO_PASSWORD = f"http://127.0.0.1:{self.aio.server_address[1]}", "watcher", "pw"
        lf.DEMO, lf.HIDE, lf.PAGE_PASSWORD, lf.SOURCE_NAMES = False, False, "", {}
        lf.history.clear()
        lf.events.clear()
        lf.state.clear()
        lf.state["ready"] = False
        lf._cache.clear()
        lf._geo.clear()
        lf._seen.update(viewers={}, feed_ok=None)
        lf._aio.update(cookie=None, retry=0, error=None)


class Feed(Base):
    def test_only_login_is_a_post_and_reads_stay_on_the_list(self):
        lf.poll()
        lf.poll()
        posts = [s for s in FakeAio.seen if s[0] == "POST"]
        self.assertEqual(posts, [("POST", "/api/v1/auth/login", None)])
        allowed = {"/api/v1" + p for p in lf.READS} | {"/api/v1/status"}
        self.assertTrue({s[1] for s in FakeAio.seen if s[0] == "GET"} <= allowed)
        with self.assertRaises(ValueError):
            lf.aio_get("/dashboard/streams/stop")

    def test_urls_keys_and_other_fields_never_reach_the_page(self):
        lf.poll()
        text = json.dumps(lf.state)
        for secret in ("SECRET-TOKEN", "SECRET-KEY", "SECRET-PATH", "SECRET-SETTING", "displayUrl", "targetKey", "abc"):
            self.assertNotIn(secret, text)

    def test_sources_and_rates(self):
        lf.SOURCE_NAMES = lf.parse_names("debrid = TorBox, stream.example.net=My addon")
        lf.poll()
        self.assertEqual([s["source"] for s in lf.state["viewers"]["streams"]], ["My addon", "TorBox", "Usenet"])
        self.assertEqual(lf.state["to_viewers"], 8.0)
        self.assertEqual(lf.state["viewers"]["streams"][0]["pos"], 10)
        self.assertEqual(lf.state["stats"]["system"]["cpu_pct"], 12)
        self.assertEqual(lf.state["stats"]["system"]["mem_used"], 5)

    def test_default_source_labels(self):
        self.assertEqual(lf.source_of("proxy", SECRET_URL), "stream.example.net")
        self.assertEqual(lf.source_of("proxy", "https://aio/debrid/playback/x"), "Debrid")
        self.assertEqual(lf.source_of("usenet", None), "Usenet")
        self.assertEqual(lf.source_of("proxy", None), "Other")

    def test_hide_viewers_is_done_on_the_server(self):
        lf.HIDE = True
        lf.poll()
        text = json.dumps(lf.state)
        for private in ("203.0.113.5", "198.51.100.9", "friend", "pal", "Film.One", "Old.Film"):
            self.assertNotIn(private, text)
        self.assertTrue(lf.state["hidden"])
        self.assertEqual(lf.state["events"][0]["text"], "Viewer started a stream")

    def test_viewer_events(self):
        lf.poll()
        self.assertTrue(any("203.0.113.5 started Film.One.mkv" == e["text"] for e in lf.events))
        lf._seen["viewers"]["gone"] = {"ip": "192.0.2.1", "file": "Gone.mkv"}
        lf.poll()
        self.assertEqual(lf.events[0]["text"], "192.0.2.1 finished Gone.mkv")


class Problems(Base):
    def error(self):
        lf.poll()
        return lf.state["viewers"]["error"]

    def test_wrong_password_tries_once_then_waits(self):
        lf.AIO_PASSWORD = "wrong"
        self.assertIn("refused the login", self.error())
        for _ in range(5):
            lf.poll()
        self.assertEqual(len([s for s in FakeAio.seen if s[0] == "POST"]), 1)
        self.assertNotIn("wrong", json.dumps(lf.state))

    def test_password_login_turned_off(self):
        FakeAio.login_status = 403
        self.assertIn("Password login is turned off", self.error())

    def test_not_an_admin_does_not_retry_every_poll(self):
        FakeAio.feed_status = 403
        self.assertIn("not an admin", self.error())
        logins = len([s for s in FakeAio.seen if s[0] == "POST"])
        lf.poll()
        self.assertEqual(len([s for s in FakeAio.seen if s[0] == "POST"]), logins)

    def test_missing_route_names_the_versions(self):
        FakeAio.feed_status = 404
        message = self.error()
        self.assertIn("9.9.9", message)
        self.assertIn(lf.TESTED_VERSION, message)

    def test_changed_format_names_the_versions(self):
        FakeAio.feed_body = {"data": {"somethingElse": []}}
        self.assertIn(lf.TESTED_VERSION, self.error())
        FakeAio.feed_body = b"<html>not json</html>"
        lf._cache.clear()
        self.assertIn(lf.TESTED_VERSION, self.error())

    def test_expired_session_logs_in_again(self):
        lf.poll()
        FakeAio.expire_next = True
        lf._aio["cookie"] = "aiostreams.session=abc"
        lf.poll()
        self.assertTrue(lf.state["viewers"]["ok"])
        self.assertEqual(len([s for s in FakeAio.seen if s[0] == "POST"]), 2)

    def test_unreachable(self):
        lf.AIO_URL = "http://127.0.0.1:9"
        self.assertIn("Can't reach AIOStreams", self.error())

    def test_redirect_is_not_followed(self):
        class Redirect(FakeAio):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.reply(302, {}, location=f"http://127.0.0.1:{FakeAio.other}/stolen")
        got = []

        class Other(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(self.path)

            do_GET = do_POST

            def log_message(self, *args):
                pass
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        FakeAio.other = other.server_address[1]
        bad = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=bad.serve_forever, daemon=True).start()
        lf.AIO_URL = f"http://127.0.0.1:{bad.server_address[1]}"
        self.assertFalse(lf.state.get("x"))
        lf.poll()
        self.assertEqual(got, [])
        for s in (other, bad):
            s.shutdown()
            s.server_close()


class Config(Base):
    def test_config_problem_names_what_is_missing(self):
        lf.AIO_PASSWORD = ""
        self.assertIn("AIOSTREAMS_PASSWORD", lf.config_problem())
        self.assertIn("FLOWS_DEMO", lf.config_problem())
        lf.DEMO = True
        self.assertIsNone(lf.config_problem())

    def test_demo_runs_the_same_code_and_stays_off_the_network(self):
        lf.start_demo()
        lf.DEMO = True
        try:
            lf.poll()
        finally:
            lf.demo = None
        self.assertTrue(lf.state["demo"])
        self.assertTrue(lf.state["viewers"]["ok"])
        self.assertEqual(lf.state["stats"]["system"]["cores"], 4)
        self.assertGreater(len(lf.state["history"]), 400)
        self.assertEqual(FakeAio.seen, [])

    def test_country_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "geo"))
            with open(os.path.join(d, "geo", "v4.csv"), "w", newline="\n") as f:
                f.write("1.0.0.0,1.0.0.255,AU\n1.0.1.0,1.0.3.255,CN\n2.0.0.0,2.0.0.255,ZZ\n8.8.8.0,8.8.8.255,US\n")
            with open(os.path.join(d, "geo", "v6.csv"), "w", newline="\n") as f:
                f.write("2001:4860::,2001:4860:ffff:ffff:ffff:ffff:ffff:ffff,US\n")
            here, lf.HERE = lf.HERE, d
            try:
                self.assertEqual(lf.country("1.0.0.5"), "au")
                self.assertEqual(lf.country("1.0.2.5"), "cn")
                self.assertEqual(lf.country("8.8.8.8"), "us")
                self.assertIsNone(lf.country("2.0.0.1"))
                self.assertIsNone(lf.country("9.9.9.9"))
                self.assertEqual(lf.country("2001:4860::1"), "us")
                self.assertIsNone(lf.country(None))
                self.assertIsNone(lf.country("not an ip"))
            finally:
                lf.HERE = here


class Page(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.page = lf.Server(("127.0.0.1", 0), lf.Handler)
        threading.Thread(target=cls.page.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.page.shutdown()
        cls.page.server_close()
        super().tearDownClass()

    def get(self, path, password=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.page.server_address[1]}{path}")
        if password is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(b"any:" + password.encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_routes(self):
        self.assertEqual(self.get("/")[0], 200)
        self.assertEqual(self.get("/api/state")[0], 200)
        self.assertEqual(self.get("/flags/no.svg")[0], 200)
        self.assertEqual(self.get("/flags/zz.svg")[0], 404)
        self.assertEqual(self.get("/flags/../live_flows.py")[0], 404)
        self.assertEqual(self.get("/live_flows.py")[0], 404)

    def test_password_covers_everything_except_health(self):
        lf.PAGE_PASSWORD = "open sesame"
        for path in ("/", "/api/state", "/flags/no.svg"):
            self.assertEqual(self.get(path)[0], 401, path)
            self.assertEqual(self.get(path, "wrong")[0], 401, path)
            self.assertEqual(self.get(path, "open sesame")[0], 200, path)
        self.assertEqual(self.get("/health")[0], 200)
        self.assertIn("Basic", self.get("/")[2]["WWW-Authenticate"])

    def test_page_loads_nothing_from_outside(self):
        body = self.get("/")[1].decode()
        self.assertNotIn("googleapis", body)
        self.assertNotIn("<link", body)
        self.assertIn("default-src 'self'", self.get("/")[2]["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
