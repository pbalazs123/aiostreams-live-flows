"""Simulated answers in the same shape as AIOStreams' dashboard feed, for FLOWS_DEMO=1.

The addresses come from the ranges reserved for documentation, and the films are free open-movie or public-domain titles.
"""
import math
import time

VERSION = "demo"
GB = 2**30
COUNTRIES = {"203.0.113.24": "no", "198.51.100.77": "hu", "192.0.2.140": "de", "2001:db8:51::8": "ro", "203.0.113.90": "se"}
# id, user, ip, file, size, url, transport, cycle seconds, share of the cycle playing, Mbit/s
SESSIONS = [
    ("a", "anna", "203.0.113.24", "Big.Buck.Bunny.2008.2160p.BluRay.REMUX.HEVC.mkv", 38 * GB, "https://x/debrid/playback/x", "proxy", 2400, 0.9, 62),
    ("b", "bence", "198.51.100.77", "Sintel.2010.1080p.BluRay.x264.mkv", 9 * GB, "https://addon.example.org/stream/x", "proxy", 1500, 0.8, 14),
    ("c", "clara", "192.0.2.140", "Tears.of.Steel.2012.1080p.WEB-DL.mkv", 6 * GB, "https://x/debrid/playback/x", "proxy", 1100, 0.7, 11),
    ("d", "dani", "2001:db8:51::8", "Night.of.the.Living.Dead.1968.1080p.BluRay.mkv", 12 * GB, "https://x/usenet/x", "usenet", 1800, 0.6, 18),
]
ADDONS = [("comet", 1840, 3, 0.2, 620), ("mediafusion", 1612, 12, 0.7, 910), ("torbox-search", 1533, 0, 0.0, 340), ("newznab", 702, 58, 8.3, 2300)]
ENDED = [
    ("Elephants.Dream.2006.1080p.BluRay.mkv", "203.0.113.90", "idle", 6, 41, 3.2),
    ("Cosmos.Laundromat.2015.1080p.WEB-DL.mkv", "198.51.100.77", "idle", 19, 12, 1.1),
    ("Spring.2019.2160p.WEB-DL.mkv", "203.0.113.24", "stale", 48, 7, 0.6),
    ("The.General.1926.1080p.BluRay.mkv", "192.0.2.140", "idle", 95, 78, 7.4),
    ("Nosferatu.1922.1080p.BluRay.mkv", "2001:db8:51::8", "stopped", 170, 22, 2.0),
]


def session(s, now):
    """What one simulated viewer is doing at time now (seconds), or None if it is between films."""
    key, user, ip, name, size, url, transport, cycle, on, mbit = s
    played = (now + cycle * "abcd".index(key) / 4 + cycle * on / 2) % cycle
    if played >= cycle * on:
        return None
    paused = played > cycle * on * 0.88
    wave = 1 + 0.28 * math.sin(now / 37 + cycle) + 0.14 * math.sin(now / 9 + cycle)
    bps = 0 if paused else int(mbit * wave * 1e6 / 8)
    served = min(int(played * mbit * 1e6 / 8), int(size * 0.97))
    return {
        "id": f"{key}{int(now - played)}", "transport": transport, "username": user, "clientIp": ip, "targetKey": "demo", "filename": name,
        "displayUrl": url, "size": size, "bytesServed": served, "requests": 1 + int(played / 300), "startedAt": int((now - played) * 1000),
        "activity": "paused" if paused else "streaming", "start": 0, "currentBytes": served, "bytesPerSec": bps,
    }


def live(now):
    streams = [x for x in (session(s, now) for s in SESSIONS) if x]
    return {"streams": streams, "summary": {"totalBytesPerSec": sum(x["bytesPerSec"] for x in streams)}}


def backfill(points, step):
    """The chart would start empty otherwise."""
    now = time.time()
    for i in range(points - 1, 0, -1):
        t = now - i * step
        yield int(t * 1000), live(t)["summary"]["totalBytesPerSec"] * 8 / 1e6


def read(path):
    now = time.time()
    if path == "/dashboard/streams/live":
        return live(now)
    if path.startswith("/dashboard/streams/history"):
        return {"entries": [{
            "id": name, "transport": "proxy", "username": "", "clientIp": ip, "filename": name, "displayUrl": "https://x/debrid/playback/x",
            "size": int(served * GB * 1.2), "bytesServed": int(served * GB), "requests": 2 + minutes // 12,
            "startedAt": int((now - (ago + minutes) * 60) * 1000), "endedAt": int((now - ago * 60) * 1000), "endReason": reason,
        } for name, ip, reason, ago, minutes, served in ENDED]}
    if path.startswith("/dashboard/streams/bandwidth"):
        day = 86400000
        today = int(now * 1000) // day * day
        series = [{"bucketMs": today - (30 - i) * day, "bytes": int((34 + 22 * math.sin(i / 2.3) + 15 * math.sin(i * 1.7)) * GB)} for i in range(31)]
        return {"total": sum(b["bytes"] for b in series), "series": series}
    if path.startswith("/dashboard/analytics/addons"):
        return {"addons": [{"presetId": n, "requests": r, "errors": e, "errorRate": p, "avgLatencyMs": ms} for n, r, e, p, ms in ADDONS]}
    if path == "/dashboard/system":
        return {"cpu": {"cores": 4, "total": int(18 + 7 * math.sin(now / 23)), "loadavg": [round(0.7 + 0.3 * math.sin(now / 41), 2)]},
                "memory": {"total": 8 * GB, "used": int(3.4 * GB)}, "disk": {"total": 80 * GB, "used": 31 * GB}}
    raise ValueError(path)
