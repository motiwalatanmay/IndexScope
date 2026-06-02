#!/usr/bin/env python3
"""Fetch India's total market cap (BSE) and recompute the Buffett indicator.

Buffett indicator = total listed market cap / nominal GDP. The numerator is the
total market cap of all BSE-listed companies, pulled live from BSE's public
MarketCapital endpoint. The denominator (nominal GDP) is not a live series —
it is released annually/quarterly by MOSPI — so it lives as the constants below
and is reconciled into data/buffett.json on every run.

Runs from GitHub Actions twice daily alongside fetch_indices.py.

To update GDP when a new MOSPI/Budget print lands, edit GDP_FY25_ACTUAL /
GDP_FY26_ESTIMATE below (₹ lakh crore) — they are the single source of truth.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# --- Nominal GDP, ₹ lakh crore (single source of truth; update on new prints) ---
# Buffett's original definition uses the latest REPORTED ACTUAL GNP/GDP (he never
# anchors on a government forecast), so TRAILING is the headline denominator.
# FORWARD is a Budget projection — shown only as a footnote, not the headline.
# Update these on each new MOSPI/Budget print (see GDP calendar in the README/UI).
GDP_TRAILING = 357.14   # FY26 (2025-26) nominal GDP — MOSPI First Advance Estimate, Jan-2026 (2011-12 base)
GDP_TRAILING_NEWBASE = 345.47  # FY26 nominal GDP on MOSPI's new 2022-23 base (Second AE, 27-Feb-2026)
GDP_PRIOR    = 330.68   # FY25 (2024-25) nominal GDP — MOSPI actual (2011-12 base)
GDP_FORWARD  = 392.85   # FY27E (2026-27) nominal GDP — Union Budget 2026-27 BE (~10% growth)
GDP_TRAILING_LABEL = "FY26 actual"
GDP_NEWBASE_LABEL = "FY26 new base (2022-23)"
GDP_PRIOR_LABEL = "FY25 actual"
GDP_FORWARD_LABEL = "FY27E"
GDP_UPDATED = "2026-06-02"
# NOTE: the whole tracker (history + headline) is kept on the 2011-12 base for internal
# consistency. When MOSPI publishes the full back-cast 2022-23-base series, migrate
# every history row's GDP and swap GDP_TRAILING -> GDP_TRAILING_NEWBASE here.
GDP_NOTE = ("Buffett uses latest reported actual GDP (no forecast); headline = trailing "
            "FY26, MOSPI FAE Jan-2026, on the 2011-12 base (357.14). MOSPI rebased to a "
            "2022-23 series on 27-Feb-2026 (FY26 = 345.47 L cr). Whole series kept on "
            "2011-12 base so the 20-yr history stays internally comparable until the "
            "back-cast new-base series is published. Prior = FY25 actual; forward = FY27 "
            "Budget BE (context only).")

# --- Corporate profit-to-GDP (decomposition), % (annual, like GDP; update on new MO prints) ---
# Identity: market cap/GDP = (profit/GDP) x (market cap/profit) = profit-share x P/E.
# Profit-share lets us split a high Buffett reading into "high because profitable"
# vs "high because expensive". Source: Motilal Oswal Nifty-500 corporate profit-to-GDP.
# Only firmly-sourced years are kept (FY09-FY19 are not published free, so omitted —
# the gauge spans the sourced trough->peak rather than plotting interpolated data).
PROFIT_SHARE = 4.7              # FY25 (latest), Nifty-500, 17-yr high
PROFIT_SHARE_FY = "FY25"
PROFIT_SHARE_PEAK = 5.2         # FY08 peak
PROFIT_SHARE_PEAK_FY = "FY08"
PROFIT_SHARE_TROUGH = 2.1       # FY20 trough (two-decade low)
PROFIT_SHARE_TROUGH_FY = "FY20"
PROFIT_SHARE_SOURCE = "Motilal Oswal Nifty-500 corporate profit-to-GDP (FY25 = 4.7%)"
PROFIT_SHARE_ANCHORS = [["FY08", 5.2], ["FY20", 2.1], ["FY23", 4.0], ["FY24", 4.8], ["FY25", 4.7]]

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "buffett.json"
IST = timezone(timedelta(hours=5, minutes=30))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

BSE_MCAP_URL = "https://api.bseindia.com/BseIndiaAPI/api/MarketCapital/w?flag=0"


def fetch_total_mcap() -> dict:
    """Return {'mcap_lakh_cr': float, 'usd_tn': float|None, 'dttm': str}."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.bseindia.com/",
        "Origin": "https://www.bseindia.com",
    })
    # Cookie warmup — BSE's WAF redirects /api/* without a prior homepage hit.
    s.get("https://www.bseindia.com/", timeout=15)
    last_err = None
    for attempt in range(3):
        try:
            r = s.get(BSE_MCAP_URL, timeout=15)
            r.raise_for_status()
            payload = r.json()
            raw = payload.get("Mktcap")
            if not raw:
                raise ValueError(f"no Mktcap in response: {payload}")
            # "4,62,67,787.61" is ₹ crore (Indian grouping). 1 lakh cr = 1e5 cr.
            mcap_cr = float(re.sub(r"[^\d.]", "", str(raw)))
            usd_raw = payload.get("MktCap_USD")
            usd_tn = float(re.sub(r"[^\d.]", "", str(usd_raw))) if usd_raw else None
            return {
                "mcap_lakh_cr": round(mcap_cr / 1e5, 2),
                "usd_tn": usd_tn,
                "dttm": payload.get("Mktcap_Dttm", ""),
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"BSE market-cap fetch failed after retries: {last_err}")


def load_doc() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"status": "ok", "history": [], "daily": []}


def upsert_daily(daily: list, date: str, row: list) -> str:
    idx = next((i for i, r in enumerate(daily) if r[0] == date), -1)
    if idx >= 0:
        daily[idx] = row
        return "updated"
    daily.append(row)
    daily.sort(key=lambda r: r[0])
    return "appended"


def main() -> int:
    snap = fetch_total_mcap()
    mcap = snap["mcap_lakh_cr"]
    now_utc = datetime.now(timezone.utc)
    fetched_at = now_utc.isoformat().replace("+00:00", "Z")
    date_ist = now_utc.astimezone(IST).strftime("%Y-%m-%d")

    ratio_trailing = round(mcap / GDP_TRAILING * 100, 1)
    ratio_prior = round(mcap / GDP_PRIOR * 100, 1)
    ratio_forward = round(mcap / GDP_FORWARD * 100, 1)

    doc = load_doc()
    doc["status"] = "ok"
    doc["fetchedAt"] = fetched_at
    doc["gdp"] = {
        "trailing": GDP_TRAILING, "trailing_label": GDP_TRAILING_LABEL,
        "trailing_newbase": GDP_TRAILING_NEWBASE, "newbase_label": GDP_NEWBASE_LABEL,
        "prior": GDP_PRIOR, "prior_label": GDP_PRIOR_LABEL,
        "forward": GDP_FORWARD, "forward_label": GDP_FORWARD_LABEL,
        "unit": "lakh_crore",
        "updated": GDP_UPDATED,
        "note": GDP_NOTE,
    }
    doc["live"] = {
        "mcap": mcap,
        "usd_tn": snap["usd_tn"],
        "dttm": snap["dttm"],
        "date": date_ist,
    }
    doc["decomp"] = {
        "profit_share": PROFIT_SHARE, "profit_share_fy": PROFIT_SHARE_FY,
        "peak": PROFIT_SHARE_PEAK, "peak_fy": PROFIT_SHARE_PEAK_FY,
        "trough": PROFIT_SHARE_TROUGH, "trough_fy": PROFIT_SHARE_TROUGH_FY,
        "anchors": PROFIT_SHARE_ANCHORS,
        "source": PROFIT_SHARE_SOURCE,
    }
    daily = doc.setdefault("daily", [])
    action = upsert_daily(daily, date_ist,
                          [date_ist, mcap, ratio_trailing, ratio_prior, ratio_forward])

    DATA_FILE.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    print(f"buffett  {action}  date={date_ist}  mcap={mcap}L cr  "
          f"ratio(trailing FY26)={ratio_trailing}%  prior(FY25)={ratio_prior}%  "
          f"forward(FY27E)={ratio_forward}%  usd={snap['usd_tn']}tn  bse_dttm={snap['dttm']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
