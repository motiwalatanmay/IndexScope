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
    "n500":    "NIFTY 500",
}

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IST = timezone(timedelta(hours=5, minutes=30))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


TIMEOUT = 25          # per-request
ATTEMPTS = 3          # attempts per source before moving on

# The IndexScope Cloudflare Worker proxies NSE /api/allIndices from Cloudflare's
# edge. That egress reaches NSE even when NSE tarpits GitHub's datacenter IPs
# (the failure mode that started 2026-07-18), so it is the PRIMARY source here;
# a direct NSE hit is the fallback for when the worker itself is down.
WORKER_URL = "https://indexscope-live.motiwalatanmay0.workers.dev/"


def _fetch_worker() -> dict:
    """Fetch via the Cloudflare worker; normalise to the NSE /api/allIndices shape."""
    r = requests.get(WORKER_URL, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    prices = (r.json() or {}).get("prices") or {}
    if not prices:
        raise RuntimeError("worker returned no prices")
    data = []
    for key, nse_name in INDICES.items():
        v = prices.get(key)
        if not v:
            continue
        row = dict(v)
        row["index"] = nse_name          # so extract()/to_row() work unchanged
        data.append(row)
    if not data:
        raise RuntimeError("worker prices had none of the tracked indices")
    return {"data": data}


def _fetch_direct() -> dict:
    """One full direct attempt: fresh session, cookie warmup, then /api/allIndices.

    NSE blocks /api/* without a prior page hit, so the ENTIRE warmup+fetch flow
    (not just the API call) is retried with fresh cookies by the caller.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    # Cookie warmup — NSE blocks /api/* without a prior page hit.
    s.get("https://www.nseindia.com/", timeout=TIMEOUT)
    s.get("https://www.nseindia.com/market-data/live-equity-market", timeout=TIMEOUT)
    r = s.get("https://www.nseindia.com/api/allIndices", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_all_indices() -> dict:
    """Worker first (datacenter-IP safe), direct NSE as fallback; each retried."""
    last_err = None
    for label, fn in (("worker", _fetch_worker), ("nse-direct", _fetch_direct)):
        for attempt in range(ATTEMPTS):
            try:
                payload = fn()
                if label != "worker" or attempt:
                    print(f"NSE data via {label} (attempt {attempt + 1})", file=sys.stderr)
                return payload
            except Exception as e:
                last_err = e
                print(f"{label} attempt {attempt + 1}/{ATTEMPTS} failed: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                if attempt < ATTEMPTS - 1:
                    time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"NSE fetch failed via all sources: {last_err}")


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
