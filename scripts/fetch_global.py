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
If one index fails, its previous global.json entry is carried forward (marked
"stale") instead of being dropped.

Daily archive (data/global_price_history.json): append-only daily closes in
local currency, [date, close] per index, dated by the exchange's local date.
The first run seeds SEED_YEARS of history; later runs add new days and may
revise only the last REVISE_DAYS (finalises partial intraday bars). Older
archived points are frozen and no write may remove a point
(see scripts/_archive_io.py). `--seed-years N` extends the archive backwards.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

from _returns import chart_returns
from _archive_io import (ArchiveError, assert_no_loss, atomic_write_json,
                         load_json_strict, merge_series)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IST = timezone(timedelta(hours=5, minutes=30))

# key, display name, region label, yahoo symbol, local currency, highlight
INDICES = [
    ("nifty",  "Nifty 50",        "India",       "^NSEI",     "INR", True),
    ("spx",    "S&P 500",         "US",          "^GSPC",     "USD", False),
    ("ndx",    "Nasdaq 100",      "US",          "^NDX",      "USD", False),
    ("em",     "MSCI EM (EEM)",   "Emerging",    "EEM",       "USD", False),
    ("csi300", "CSI 300 (ETF)",   "China",       "510300.SS", "CNY", False),  # Yahoo serves only 1 day for ^000300.SS; 510300 ETF tracks it in CNY
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


SEED_YEARS = 3
PRICE_HIST = "global_price_history.json"


def daily_close(sym: str):
    """Full daily close series (pandas Series, exchange-local index) or None."""
    h = yf.Ticker(sym).history(period="max", interval="1d", auto_adjust=False)
    if h.empty:
        return None
    return h["Close"].dropna()


def weekly_close(sym: str, daily=None):
    """Friday-anchored weekly close series as {date_str: float}."""
    s = daily if daily is not None else daily_close(sym)
    if s is None or s.empty:
        return {}
    s = s.resample("W-FRI").last().dropna()
    out = {d.strftime("%Y-%m-%d"): round(float(v), 4) for d, v in s.items()}
    # The in-progress week resamples to a future Friday. Stamp it with today's
    # IST date so no point is dated in the future (all series share this label).
    cap = datetime.now(timezone.utc).astimezone(IST).strftime("%Y-%m-%d")
    if out:
        last = max(out)
        if last > cap:
            out[cap] = out.pop(last)
    return out


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


def update_price_archive(daily_by_key: dict, today: str, seed_years: int | None) -> int:
    """Merge fetched daily closes into the append-only archive. Returns exit code."""
    path = DATA_DIR / PRICE_HIST
    try:
        old = load_json_strict(path)
    except ArchiveError as e:
        print(f"FATAL: {e}; archive left untouched", file=sys.stderr)
        return 1
    doc = old or {
        "schema": 1,
        "metric": "daily close, local currency, price only (auto_adjust=False)",
        "dateConvention": "exchange-local trading date as served by Yahoo",
        "source": "Yahoo Finance via yfinance (scripts/fetch_global.py)",
        "note": "Append-only. Points older than the revise window are frozen; "
                "no run removes a point. Rows are [date, close].",
        "manifest": {}, "flags": {}, "byKey": {},
    }
    old_by_key = {k: list(v) for k, v in doc.get("byKey", {}).items()}
    by_key = doc.setdefault("byKey", {})
    manifest = doc.setdefault("manifest", {})
    meta = {k: (sym, ccy, name) for k, name, _r, sym, ccy, _h in INDICES}
    default_start = (datetime.fromisoformat(today) - timedelta(days=365 * SEED_YEARS)).strftime("%Y-%m-%d")
    for key, pts in daily_by_key.items():
        cur = by_key.get(key, [])
        if seed_years:
            start = (datetime.fromisoformat(today) - timedelta(days=365 * seed_years)).strftime("%Y-%m-%d")
            if cur:
                start = min(start, cur[0][0])
        else:
            start = cur[0][0] if cur else default_start
        new = [[d, v] for d, v in pts if start <= d <= today]
        merged, st = merge_series(cur, new, today)
        by_key[key] = merged
        sym, ccy, name = meta[key]
        m = manifest.setdefault(key, {})
        m.update({"name": name, "sym": sym, "cur": ccy,
                  "definition": f"{name} daily close in {ccy} (Yahoo {sym})",
                  "source": "Yahoo Finance via yfinance",
                  "method": "seed: yfinance daily history; then daily append by fetch_global.py",
                  "firstDate": merged[0][0] if merged else None,
                  "lastDate": merged[-1][0] if merged else None,
                  "n": len(merged)})
        m.setdefault("seededOn", today)
        # flag day-over-day moves > 40% so the validator can tell a real
        # event from a bad print; flags are only ever added, never removed
        fl = doc.setdefault("flags", {}).setdefault(key, [])
        have = {f[0] for f in fl}
        for (d0, v0), (d1, v1) in zip(merged, merged[1:]):
            if v0 and abs(v1 / v0 - 1) > 0.40 and d1 not in have:
                fl.append([d1, f"jump {v0}->{v1} ({(v1 / v0 - 1) * 100:+.1f}%), auto-flagged, unreviewed"])
        print(f"  archive {key:>7}: {len(cur):5d} -> {len(merged):5d}  "
              f"(+{st['added']} new, {st['revised']} revised, {st['frozen_diff']} frozen-diff kept)")
    doc["flags"] = {k: v for k, v in doc.get("flags", {}).items() if v}
    doc["lastUpdated"] = today
    try:
        assert_no_loss(old_by_key, by_key, PRICE_HIST)
    except ArchiveError as e:
        print(f"FATAL: {e}; archive left untouched", file=sys.stderr)
        return 1
    atomic_write_json(path, doc)
    print(f"wrote data/{PRICE_HIST}  ({sum(len(v) for v in by_key.values())} points, "
          f"{path.stat().st_size / 1024:.0f} KB)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-years", type=int, default=None,
                    help="extend the daily archive back this many years")
    args = ap.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    today_ist = now_utc.astimezone(IST)

    fx_cache: dict[str, dict | None] = {}
    out = []
    failures = []
    daily_by_key: dict[str, list] = {}
    for key, name, region, sym, ccy, hi in INDICES:
        try:
            daily = daily_close(sym)
            if daily is not None and len(daily) >= 10:
                daily_by_key[key] = [[d.strftime("%Y-%m-%d"), round(float(v), 4)]
                                     for d, v in daily.items()]
            local = weekly_close(sym, daily)
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
            # The in-progress week is labelled with today's IST date (shared chart
            # label); lastDate must be the trading date of the last close instead.
            if daily is not None and len(daily):
                last_d = daily.index[-1].strftime("%Y-%m-%d")
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

    # Carry forward the last good entry for any index that failed this run, so
    # a single Yahoo hiccup cannot drop a card from global.json.
    try:
        prev = load_json_strict(DATA_DIR / "global.json", default={}) or {}
    except ArchiveError as e:
        print(f"WARN: {e}; no carry-forward possible", file=sys.stderr)
        prev = {}
    got = {r["key"] for r in out}
    order = [k for k, *_ in INDICES]
    for r in prev.get("indices", []):
        if r.get("key") not in got and r.get("key") in order:
            r = dict(r, stale=True)
            out.append(r)
            print(f"  carried forward {r['key']} from {prev.get('asOf')} (fetch failed)")
    out.sort(key=lambda r: order.index(r["key"]))

    # Archive first, so the table's returns come from the same daily rows the
    # chart draws (same anchor rule as index.html glRenderPx; see _returns.py).
    rc = update_price_archive(daily_by_key, today_ist.strftime("%Y-%m-%d"), args.seed_years)
    try:
        arch = (load_json_strict(DATA_DIR / PRICE_HIST, default={}) or {}).get("byKey", {})
    except ArchiveError as e:
        print(f"WARN: {e}; table returns stay on the weekly series", file=sys.stderr)
        arch = {}
    for r in out:
        rows = arch.get(r["key"])
        if r.get("stale") or not rows:
            continue
        # The archive holds ~3y so far; older years come from this run's full
        # Yahoo daily series. Archive rows win where both exist, so 1Y/3Y are
        # computed on exactly the rows the chart draws.
        merged = dict(daily_by_key.get(r["key"], []))
        merged.update({d: v for d, v in rows})
        rows = sorted(merged.items())
        # Header close/date = the chart's last point (Yahoo can drop a bar the
        # archive already holds, e.g. a NaN close on the latest session).
        r["lastDate"], r["last"] = rows[-1][0], rows[-1][1]
        r["ret"] = chart_returns(rows)
        fx = fx_cache.get(r["cur"])
        r["retUsd"] = r["ret"] if fx is None else chart_returns(rows, fx, usd=True)
        r["retBasis"] = "daily archive, chart anchor"

    doc = {
        "status": "ok",
        "fetchedAt": now_utc.isoformat().replace("+00:00", "Z"),
        "asOf": today_ist.strftime("%Y-%m-%d"),
        "note": "Chart series are weekly closes. Returns are price-only, in local ccy and USD, on the chart's range rule: base = first daily close on or after the same calendar date N years before the last close; YTD base = first close of the calendar year. YTD/1Y/3Y come from data/global_price_history.json and can be recomputed from it. 5Y/10Y use that run's full Yahoo daily series (archive rows win where both exist) because the archive starts in 2023; they cannot be recomputed from repo files.",
        "indices": out,
    }
    atomic_write_json(DATA_DIR / "global.json", doc)
    print(f"\nwrote data/global.json  ({len(out)} indices)")
    if failures:
        print("WARN:", *failures, sep="\n  ", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
