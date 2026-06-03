#!/usr/bin/env python3
"""Build NIFTY 500 daily history (PE/PB/DivYield) from NSE archive bhavcopies.

Source: https://nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv
Each file is the all-indices close for one trading day, including P/E, P/B, Div Yield.
The 500 index is named "CNX 500" before ~Nov-2015 and "Nifty 500" after.

Output: data/n500_history.json  -> [[date, close, pe, pb, dy], ...] sorted ascending.

Resumable: re-running only fetches dates not already present. Non-trading days
(weekends/holidays return HTML or 404) are recorded as "skipped" so we don't refetch.
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT = DATA_DIR / "n500_history.json"
SKIP = DATA_DIR / "n500_history_skip.json"  # cache of known non-trading days

START = date(2013, 1, 1)
NAMES = {"nifty 500", "cnx 500"}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MAX_WORKERS = 12


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://www.nseindia.com/"})
    return s


def fetch_day(s: requests.Session, d: date):
    """Return ('row', [date,close,pe,pb,dy]) | ('skip', None) | ('retry', None)."""
    url = f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{d:%d%m%Y}.csv"
    try:
        r = s.get(url, timeout=20)
    except Exception:
        return ("retry", None)
    if r.status_code == 404:
        return ("skip", None)
    if r.status_code != 200:
        return ("retry", None)
    text = r.text
    if not text.lstrip().lower().startswith("index name"):
        return ("skip", None)  # HTML / holiday placeholder
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if row[0].strip().lower() in NAMES:
            def num(x):
                x = (x or "").strip()
                if x in ("", "-"):
                    return None
                try:
                    return float(x)
                except ValueError:
                    return None
            close, pe, pb, dy = num(row[5]), num(row[10]), num(row[11]), num(row[12])
            if close is None:
                return ("skip", None)
            return ("row", [d.isoformat(), close, pe, pb, dy])
    return ("skip", None)  # CSV present but no 500 row that day


def load_json(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return default
    return default


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = {r[0]: r for r in load_json(OUT, [])}
    skips = set(load_json(SKIP, []))
    today = date.today()

    todo = []
    d = START
    while d <= today:
        if d.weekday() < 5:  # Mon-Fri only
            iso = d.isoformat()
            if iso not in rows and iso not in skips:
                todo.append(d)
        d += timedelta(days=1)

    print(f"have {len(rows)} rows, {len(skips)} known skips; {len(todo)} dates to fetch")
    if not todo:
        print("nothing to do")
        return 0

    sessions = [make_session() for _ in range(MAX_WORKERS)]
    retry: list[date] = []
    done = 0

    def work(idx_d):
        i, dd = idx_d
        return dd, fetch_day(sessions[i % MAX_WORKERS], dd)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(work, (i, dd)) for i, dd in enumerate(todo)]
        for fut in as_completed(futs):
            dd, (kind, payload) = fut.result()
            if kind == "row":
                rows[payload[0]] = payload
            elif kind == "skip":
                skips.add(dd.isoformat())
            else:
                retry.append(dd)
            done += 1
            if done % 250 == 0:
                print(f"  ...{done}/{len(todo)}  rows={len(rows)}")

    # one retry pass, sequential & polite
    if retry:
        print(f"retrying {len(retry)} dates sequentially...")
        s = make_session()
        for dd in retry:
            kind, payload = fetch_day(s, dd)
            if kind == "row":
                rows[payload[0]] = payload
            elif kind == "skip":
                skips.add(dd.isoformat())
            time.sleep(0.15)

    out = sorted(rows.values(), key=lambda r: r[0])
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    SKIP.write_text(json.dumps(sorted(skips), separators=(",", ":")))
    print(f"WROTE {OUT}  rows={len(out)}  range={out[0][0]}..{out[-1][0]}")
    # quick sanity
    miss_pe = sum(1 for r in out if r[2] is None)
    print(f"  rows missing PE: {miss_pe}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
