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
        # Format: "Current Shiller PE Ratio : 42.84 ..."
        m = re.search(r"Current[^0-9]*([0-9]+\.[0-9]+)", txt)
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

    # India: override with the authoritative NSE Nifty 50 P/E (same number the
    # Dashboard tab shows) instead of worldperatio's broad-India basket, so the
    # two tabs reconcile. Cross-check: worldperatio India ~23 vs NSE Nifty ~20.
    try:
        n50 = json.loads((DATA_DIR / "n50.json").read_text())
        rows = n50.get("data", [])
        last_pe = next((r[2] for r in reversed(rows) if len(r) > 2 and r[2]), None)
        if last_pe:
            for rec in out:
                if rec["key"] == "nifty":
                    rec["pe"] = round(float(last_pe), 2)
                    rec["verdict"] = verdict(rec["pe"], rec["refLo"], rec["refHi"])
                    rec["src"] = "NSE"
                    print(f"  nifty override -> NSE Nifty 50 P/E {rec['pe']} ({rec['verdict']})")
    except Exception as e:
        print(f"WARN: nifty NSE override failed: {e}", file=sys.stderr)

    # Data-driven bands: for non-India markets set the fair band to the p25-p75
    # of that market's OWN accruing P/E history (the metric-consistent
    # worldperatio Wayback seed). Auto-matures as the daily scrape extends it.
    # India stays on its static NSE-appropriate band — its history series is a
    # different (broad-India) basket, so banding the Nifty 50 number against it
    # would be a splice error. India's real percentile lives on the Dashboard.
    def _pctl(vals, p):
        v = sorted(vals)
        k = (len(v) - 1) * p / 100
        f = int(k)
        c = min(f + 1, len(v) - 1)
        return v[f] + (v[c] - v[f]) * (k - f)
    try:
        hist = json.loads((DATA_DIR / "global_pe_history.json").read_text()).get("byKey", {})
    except Exception:
        hist = {}
    for rec in out:
        if rec["key"] == "nifty":
            rec["bandNote"] = "long-run ref"
            continue
        ser = hist.get(rec["key"], [])
        vals = [v for _, v in ser]
        if len(vals) >= 8:
            lo, hi = round(_pctl(vals, 25), 1), round(_pctl(vals, 75), 1)
            if hi - lo < 3:                       # floor the spread so it isn't hair-trigger
                mid = (lo + hi) / 2
                lo, hi = round(mid - 1.5, 1), round(mid + 1.5, 1)
            rec["refLo"], rec["refHi"] = lo, hi
            rec["verdict"] = verdict(rec["pe"], lo, hi)
            rec["bandNote"] = f"own range since {ser[0][0][:4]}"
        else:
            rec["bandNote"] = "long-run ref"

    doc = {
        "status": "ok",
        "fetchedAt": now_utc.isoformat().replace("+00:00", "Z"),
        "asOf": now_utc.astimezone(IST).strftime("%Y-%m-%d"),
        "source": "worldperatio.com country baskets (trailing P/E) · India=NSE Nifty 50 · US CAPE=multpl.com",
        "note": "Trailing P/E. Non-India figures use worldperatio's consistent "
                "country-basket methodology (operating earnings) for cross-country "
                "comparability — these are broad baskets and can differ from a single "
                "headline index. India uses NSE Nifty 50 to match the Dashboard. "
                "Bands are static long-run references, not own-history percentiles.",
        "indices": out,
    }
    (DATA_DIR / "global_val.json").write_text(
        json.dumps(doc, separators=(",", ":")) + "\n")
    print(f"\nwrote data/global_val.json  ({len(out)} indices)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
