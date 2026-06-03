#!/usr/bin/env python3
"""Fetch global index history via yfinance and write data/global.json.

Why yfinance (not raw Stooq/Yahoo URLs): the library performs Yahoo's
cookie + crumb handshake and backoff, so it works from GitHub Actions where
plain curl gets 429'd and Stooq's bulk CSV now requires an API key.

Output (data/global.json) carries, per index:
  - weekly close series (~10yr) for the rebased performance chart (keeps the
    file light vs daily)
  - precomputed total returns (ytd/1y/3y/5y/10y) in BOTH local currency and
    USD. USD matters: in common-currency terms India's underperformance is
    larger (rupee depreciation), which is the honest cross-market read.

Idempotent: always rebuilds from a fresh `max` pull, so re-runs self-heal.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IST = timezone(timedelta(hours=5, minutes=30))

# key, display name, region label, yahoo symbol, local currency, highlight
INDICES = [
    ("nifty",  "Nifty 50",        "India",       "^NSEI",     "INR", True),
    ("spx",    "S&P 500",         "US",          "^GSPC",     "USD", False),
    ("ndx",    "Nasdaq 100",      "US",          "^NDX",      "USD", False),
    ("em",     "MSCI EM (EEM)",   "Emerging",    "EEM",       "USD", False),
    ("csi300", "CSI 300",         "China",       "000300.SS", "CNY", False),
    ("hsi",    "Hang Seng",       "Hong Kong",   "^HSI",      "HKD", False),
    ("nikkei", "Nikkei 225",      "Japan",       "^N225",     "JPY", False),
    ("kospi",  "KOSPI",           "South Korea", "^KS11",     "KRW", False),
    ("taiex",  "TAIEX",           "Taiwan",      "^TWII",     "TWD", False),
    ("estoxx", "Euro Stoxx 50",   "Europe",      "^STOXX50E", "EUR", False),
    ("ftse",   "FTSE 100",        "UK",          "^FTSE",     "GBP", False),
    ("gold",   "Gold (spot)",     "Commodity",   "GC=F",      "USD", False),
    ("dxy",    "US Dollar (DXY)", "FX",          "DX-Y.NYB",  "USD", False),
]

# usd_per_local FX. Yahoo conventions differ: most are "<CCY>=X" = local-per-USD
# (so invert), but EUR/GBP are quoted as "<CCY>USD=X" = usd-per-local (direct).
FX = {
    "INR": ("INR=X",     "invert"),
    "JPY": ("JPY=X",     "invert"),
    "KRW": ("KRW=X",     "invert"),
    "TWD": ("TWD=X",     "invert"),
    "HKD": ("HKD=X",     "invert"),
    "CNY": ("CNY=X",     "invert"),
    "EUR": ("EURUSD=X",  "direct"),
    "GBP": ("GBPUSD=X",  "direct"),
    "USD": (None,        "direct"),
}


def weekly_close(sym: str):
    """Friday-anchored weekly close series as {date_str: float}."""
    h = yf.Ticker(sym).history(period="max", interval="1d", auto_adjust=False)
    if h.empty:
        return {}
    s = h["Close"].dropna()
    s = s.resample("W-FRI").last().dropna()
    return {d.strftime("%Y-%m-%d"): round(float(v), 4) for d, v in s.items()}


def usd_per_local_series(ccy: str):
    """Weekly usd-per-1-unit-of-local-currency, or None for USD."""
    pair, mode = FX[ccy]
    if pair is None:
        return None
    raw = weekly_close(pair)
    if not raw:
        return None
    if mode == "invert":
        return {d: (1.0 / v) for d, v in raw.items() if v}
    return raw


def ffill_lookup(fx: dict, date: str):
    """Nearest FX value at-or-before `date` (weekly series; forward-fill)."""
    if fx is None:
        return 1.0
    if date in fx:
        return fx[date]
    keys = [k for k in fx if k <= date]
    return fx[max(keys)] if keys else None


def ret(series_items, anchor_date: str):
    """Total return (%) from the close at-or-after anchor_date to the latest."""
    if not series_items:
        return None
    last_v = series_items[-1][1]
    base = next((v for d, v in series_items if d >= anchor_date), None)
    if not base:
        return None
    return round((last_v / base - 1.0) * 100, 1)


def build_returns(series_items, today: datetime):
    y = today.year
    anchors = {
        "ytd": f"{y}-01-01",
        "1y":  (today - timedelta(days=365)).strftime("%Y-%m-%d"),
        "3y":  (today - timedelta(days=365 * 3)).strftime("%Y-%m-%d"),
        "5y":  (today - timedelta(days=365 * 5)).strftime("%Y-%m-%d"),
        "10y": (today - timedelta(days=365 * 10)).strftime("%Y-%m-%d"),
    }
    return {k: ret(series_items, a) for k, a in anchors.items()}


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    today_ist = now_utc.astimezone(IST)

    fx_cache: dict[str, dict | None] = {}
    out = []
    failures = []
    for key, name, region, sym, ccy, hi in INDICES:
        try:
            local = weekly_close(sym)
            if len(local) < 10:
                failures.append(f"{key}: thin series ({len(local)} pts)")
                continue
            local_items = sorted(local.items())

            # USD-converted weekly series
            if ccy not in fx_cache:
                fx_cache[ccy] = usd_per_local_series(ccy)
            fx = fx_cache[ccy]
            usd_items = []
            for d, v in local_items:
                f = ffill_lookup(fx, d)
                if f is not None:
                    usd_items.append((d, round(v * f, 4)))

            # Chart only needs ~11yr of weekly points; keep the file light.
            # Returns above are computed on the FULL series, so trimming the
            # stored chart series doesn't affect 10y numbers.
            chart_local = local_items[-572:]

            last_d, last_v = local_items[-1]
            prev_v = local_items[-2][1] if len(local_items) > 1 else last_v
            out.append({
                "key": key, "name": name, "region": region,
                "sym": sym, "cur": ccy, "highlight": hi,
                "last": last_v, "lastDate": last_d,
                "chgWk": round((last_v / prev_v - 1.0) * 100, 2) if prev_v else 0.0,
                "ret":    build_returns(local_items, today_ist),
                "retUsd": build_returns(usd_items, today_ist) if usd_items else None,
                # store series as [date, local, usd] for the chart's two modes
                "series": [
                    [d, v, (dict(usd_items).get(d))]
                    for d, v in chart_local
                ],
            })
            print(f"{key:>7}  pts={len(local_items):4d}  last={last_v}  "
                  f"5yL={out[-1]['ret']['5y']}%  5yUSD={(out[-1]['retUsd'] or {}).get('5y')}%")
        except Exception as e:
            failures.append(f"{key} ({sym}): {str(e)[:80]}")

    if not out:
        print("FATAL: no indices fetched", file=sys.stderr)
        return 1

    doc = {
        "status": "ok",
        "fetchedAt": now_utc.isoformat().replace("+00:00", "Z"),
        "asOf": today_ist.strftime("%Y-%m-%d"),
        "note": "Weekly closes. Returns are price-only, in local ccy and USD.",
        "indices": out,
    }
    (DATA_DIR / "global.json").write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    print(f"\nwrote data/global.json  ({len(out)} indices)")
    if failures:
        print("WARN:", *failures, sep="\n  ", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
