#!/usr/bin/env python3
"""Download DB-IP's free country file and split it for the lookup in live_flows.py. Runs once, while the image is built."""
import datetime
import gzip
import ipaddress
import os
import sys
import urllib.error
import urllib.request

URL = "https://download.db-ip.com/free/dbip-country-lite-{}.csv.gz"
MIN_ROWS = 100000


def months():
    """This month's file may not be out yet on the first of the month."""
    today = datetime.date.today()
    yield today.strftime("%Y-%m")
    yield (today.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")


def download():
    for month in months():
        try:
            req = urllib.request.Request(URL.format(month), headers={"User-Agent": "aiostreams-live-flows"})
            with urllib.request.urlopen(req, timeout=120) as r:
                print("DB-IP file", month)
                return gzip.decompress(r.read()).decode()
        except urllib.error.HTTPError as e:
            print("DB-IP file", month, "status", e.code)
    sys.exit("No DB-IP file found")


def main(target):
    os.makedirs(target, exist_ok=True)
    rows = {"v4": [], "v6": []}
    for line in download().splitlines():
        start, end, cc = line.strip().split(",")
        rows["v6" if ":" in start else "v4"].append((int(ipaddress.ip_address(start)), f"{start},{end},{cc}\n"))
    for name, data in rows.items():
        if len(data) < MIN_ROWS:
            sys.exit(f"Only {len(data)} {name} rows: refusing a damaged file")
        # The lookup is a binary search, so the order has to be right.
        data.sort(key=lambda r: r[0])
        with open(os.path.join(target, name + ".csv"), "w", encoding="ascii", newline="\n") as f:
            f.writelines(r[1] for r in data)
        print(name, len(data), "rows")


if __name__ == "__main__":
    main(sys.argv[1])
