#!/usr/bin/env python3
"""Fetch NIFTY index snapshots from NSE /api/allIndices and upsert into data/{key}.json.

Runs from GitHub Actions twice daily. Replaces the prior chain of
Apps Script + Trendlyne regex + Cloudflare Workers with one official source.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

INDICES = {
    "n50":     "NIFTY 50",
    "nn50":    "NIFTY NEXT 50",
    "nmid150": "NIFTY MIDCAP 150",
    "sc250":   "NIFTY SMALLCAP 250",
}

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IST = timezone(timedelta(hours=5, minutes=30))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def fetch_all_indices() -> dict:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    # Cookie warmup — NSE blocks /api/* without a prior page hit.
    s.get("https://www.nseindia.com/", timeout=15)
    s.get("https://www.nseindia.com/market-data/live-equity-market", timeout=15)
    last_err = None
    for attempt in range(3):
        try:
            r = s.get("https://www.nseindia.com/api/allIndices", timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"NSE fetch failed after retries: {last_err}")


def extract(payload: dict, nse_name: str) -> dict | None:
    for row in payload.get("data", []):
        if row.get("index") == nse_name:
            return row
    return None


def to_row(snap: dict, date_ist: str) -> list:
    def f(k):
        v = snap.get(k)
        try:
            return float(v) if v not in (None, "", "-") else None
        except (TypeError, ValueError):
            return None
    return [date_ist, f("last"), f("pe"), f("pb"), f("dy")]


def upsert(path: Path, new_row: list, fetched_at: str) -> tuple[str, int]:
    if path.exists():
        doc = json.loads(path.read_text())
    else:
        doc = {"status": "ok", "fetchedAt": fetched_at, "data": []}
    rows = doc.get("data", [])
    date = new_row[0]
    idx = next((i for i, r in enumerate(rows) if r[0] == date), -1)
    if idx >= 0:
        rows[idx] = new_row
        action = "updated"
    else:
        rows.append(new_row)
        rows.sort(key=lambda r: r[0])
        action = "appended"
    doc["data"] = rows
    doc["fetchedAt"] = fetched_at
    doc["status"] = "ok"
    doc["lastDate"] = rows[-1][0] if rows else None
    doc["rowCount"] = len(rows)
    path.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    return action, len(rows)


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = fetch_all_indices()
    now_utc = datetime.now(timezone.utc)
    fetched_at = now_utc.isoformat().replace("+00:00", "Z")
    date_ist = now_utc.astimezone(IST).strftime("%Y-%m-%d")

    failures: list[str] = []
    for key, nse_name in INDICES.items():
        snap = extract(payload, nse_name)
        if not snap:
            failures.append(f"{key} ({nse_name}): not found in response")
            continue
        row = to_row(snap, date_ist)
        if row[1] is None:
            failures.append(f"{key} ({nse_name}): missing 'last' value")
            continue
        action, n = upsert(DATA_DIR / f"{key}.json", row, fetched_at)
        print(f"{key:>8}  {action}  date={row[0]}  last={row[1]}  pe={row[2]}  pb={row[3]}  dy={row[4]}  rows={n}")

    if failures:
        print("FAILURES:", *failures, sep="\n  ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
