#!/usr/bin/env python3
"""Fetch current valuation (trailing P/E + US CAPE) for global indices.

Sources (both free, no key):
  - worldperatio.com  -> current trailing P/E per country (embedded JSON)
  - multpl.com        -> S&P 500 Shiller CAPE (US only; no free CAPE for others)

There is NO free *historical* P/E series for foreign indices, so unlike the
India cards (which percentile-rank against their own 10yr history), these use
STATIC long-run reference bands (lo/hi) to derive a cheap/fair/expensive
verdict. Bands are judgment calls on ~10-15yr norms — refresh occasionally.
Output: data/global_val.json.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# our index key -> (worldperatio country | special, long-run ref band lo, hi)
# estoxx uses the Germany/France mean as a euro-area proxy (no aggregate row).
MAP = {
    "nifty":  ("India",         19.0, 24.0),
    "spx":    ("United States", 17.0, 22.0),
    "csi300": ("China",         11.0, 15.0),
    "hsi":    ("Hong Kong",     10.0, 14.0),
    "nikkei": ("Japan",         15.0, 20.0),
    "kospi":  ("South Korea",   10.0, 14.0),
    "taiex":  ("Taiwan",        14.0, 18.0),
    "ftse":   ("United Kingdom",13.0, 16.0),
    "estoxx": ("__euro__",      14.0, 18.0),
}


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def country_pes() -> dict:
    html = get("https://worldperatio.com/")
    pairs = re.findall(
        r'"name":\s*"([^"]+)"(?:[^{}]*?)"desc":\s*\'[^\']*?([0-9]+\.[0-9]+)', html)
    return {k: float(v) for k, v in pairs}


def sp_cape() -> float | None:
    try:
        txt = re.sub(r"<[^>]+>", " ", get("https://www.multpl.com/shiller-pe"))
        m = re.search(r"Current\s*([0-9]+\.[0-9]+)", txt)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def verdict(pe: float, lo: float, hi: float) -> str:
    if pe < lo:
        return "cheap"
    if pe > hi:
        return "expensive"
    return "fair"


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    pes = country_pes()
    if not pes:
        print("FATAL: worldperatio returned no P/E data", file=sys.stderr)
        return 1
    euro = None
    if "Germany" in pes and "France" in pes:
        euro = round((pes["Germany"] + pes["France"]) / 2, 2)

    out = []
    for key, (country, lo, hi) in MAP.items():
        pe = euro if country == "__euro__" else pes.get(country)
        if pe is None:
            print(f"WARN: no P/E for {key} ({country})", file=sys.stderr)
            continue
        rec = {
            "key": key, "pe": round(pe, 2),
            "refLo": lo, "refHi": hi,
            "verdict": verdict(pe, lo, hi),
        }
        if key == "spx":
            cape = sp_cape()
            if cape:
                rec["cape"] = cape
        out.append(rec)
        print(f"{key:>7}  pe={rec['pe']:>5}  band={lo}-{hi}  -> {rec['verdict']}"
              + (f"  cape={rec.get('cape')}" if rec.get("cape") else ""))

    doc = {
        "status": "ok",
        "fetchedAt": now_utc.isoformat().replace("+00:00", "Z"),
        "asOf": now_utc.astimezone(IST).strftime("%Y-%m-%d"),
        "source": "worldperatio.com (trailing P/E) + multpl.com (US CAPE)",
        "note": "Trailing P/E. Bands are static long-run references, not "
                "own-history percentiles. Manual periodic refresh.",
        "indices": out,
    }
    (DATA_DIR / "global_val.json").write_text(
        json.dumps(doc, separators=(",", ":")) + "\n")
    print(f"\nwrote data/global_val.json  ({len(out)} indices)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
