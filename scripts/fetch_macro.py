#!/usr/bin/env python3
"""Fetch macro series (USD/INR, Brent, Nifty) via yfinance -> data/macro.json.

Series (weekly, Friday-anchored, ~11y; the in-progress week is dated today IST):
  usdinr  INR=X   rupees per US dollar (a RISE means a weaker rupee)
  brent   BZ=F    Brent FRONT-MONTH FUTURES in USD/bbl. It is not spot and it
                  rolls, so it can differ from EIA/FRED spot by a few percent.
  nifty   ^NSEI   Nifty 50 price index in INR

For each of usdinr / brent the file carries the level, the 12-month change, that
change's percentile against its own 10y of rolling 52-week changes, and a word
state. One rule for every macro chip (the page prints it):
  change percentile >= 75  -> HEADWIND   (India is a net oil importer and a net
                                           recipient of portfolio flows, so a
                                           weaker rupee and dearer oil both hurt)
  change percentile <= 25  -> TAILWIND
  otherwise                -> NEUTRAL
Correlations are of weekly returns and are COINCIDENT: the file also stores the
one-week-ahead correlation so the page can show that no lead exists. The rolling
52-week correlation keeps 10 years (520 points); a correlation that is undefined
(too few points, or a flat series) is written as null, never 0.0.
`lastDailyBar` is the date of the last daily quote on or before `asOf` (the quote
behind `last`); for the in-progress week it is an intraday/last quote, not a close.
`--out PATH` writes another file (testing); default data/macro.json.

Exit codes: 0 ok, 1 thin/stale data (file NOT overwritten).
Idempotent: rebuilds from a fresh `max` pull.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

DATA = Path(__file__).resolve().parent.parent / "data" / "macro.json"
IST = timezone(timedelta(hours=5, minutes=30))
MIN_WEEKS = 450          # ~8.6y; below this the percentile and correlation are not meaningful
MAX_STALE_DAYS = 8       # newest daily bar older than this = Yahoo gap, refuse to write
ROLL_KEEP = 10 * 52      # rolling-correlation points kept: 10 years of weekly windows

SERIES = {
    "usdinr": ("INR=X", "Rupees per US dollar (rise = weaker rupee)", "yfinance INR=X"),
    "brent": ("BZ=F", "Brent front-month futures, USD per barrel (not spot; rolls monthly)", "yfinance BZ=F"),
    "nifty": ("^NSEI", "Nifty 50 price index, INR", "yfinance ^NSEI"),
}


def pull(sym: str):
    h = yf.Ticker(sym).history(period="max", interval="1d", auto_adjust=False)
    s = h["Close"].dropna()
    if s.empty:
        return None, None
    days = [d.strftime("%Y-%m-%d") for d in s.index]
    w = s.resample("W-FRI").last().dropna()
    cap = datetime.now(timezone.utc).astimezone(IST).strftime("%Y-%m-%d")
    out = {d.strftime("%Y-%m-%d"): float(v) for d, v in w.items()}
    if out and max(out) > cap:
        out[cap] = out.pop(max(out))
    return out, days


def pctile_rank(values, x):
    """Share of `values` that are <= x, 0..100."""
    vs = [v for v in values if v is not None and not math.isnan(v)]
    if not vs:
        return None
    return round(100.0 * sum(1 for v in vs if v <= x) / len(vs), 1)


def corr(a, b):
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    if n < 20:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def r3(x):
    """Round a correlation to 3 dp; None (undefined) stays None so the JSON says null, never 0.0."""
    return None if x is None else round(x, 3)


def last_bar_on_or_before(days, cutoff):
    """Newest daily-bar date <= cutoff (days are sorted ascending ISO strings)."""
    prior = [d for d in days if d <= cutoff]
    return prior[-1] if prior else None


def state_for(pct):
    if pct is None:
        return "UNKNOWN"
    if pct >= 75:
        return "HEADWIND"
    if pct <= 25:
        return "TAILWIND"
    return "NEUTRAL"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch macro series via yfinance -> data/macro.json")
    ap.add_argument("--out", type=Path, default=DATA, help="file to write (default data/macro.json)")
    out_path = ap.parse_args(argv).out
    now = datetime.now(timezone.utc)
    raw, last_days = {}, {}
    for key, (sym, _d, _s) in SERIES.items():
        try:
            raw[key], last_days[key] = pull(sym)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {key} {sym}: {e}", file=sys.stderr)
            return 1
        if not raw[key] or len(raw[key]) < MIN_WEEKS:
            print(f"FAIL {key}: thin series ({len(raw[key] or {})} weeks)", file=sys.stderr)
            return 1
        age = (now.date() - datetime.strptime(last_days[key][-1], "%Y-%m-%d").date()).days
        if age > MAX_STALE_DAYS:
            print(f"FAIL {key}: newest bar {last_days[key][-1]} is {age} days old", file=sys.stderr)
            return 1

    dates = sorted(set(raw["usdinr"]) & set(raw["brent"]) & set(raw["nifty"]))[-572:]
    if len(dates) < MIN_WEEKS:
        print(f"FAIL: only {len(dates)} common weeks", file=sys.stderr)
        return 1
    fx = [raw["usdinr"][d] for d in dates]
    br = [raw["brent"][d] for d in dates]
    nf = [raw["nifty"][d] for d in dates]

    def rets(x):
        return [math.log(x[i] / x[i - 1]) for i in range(1, len(x))]
    r_fx, r_br, r_nf = rets(fx), rets(br), rets(nf)   # r[i] is the return into dates[i+1]

    # 12M change series (52 weeks) and its percentile for each chip.
    def chg52(x):
        return [None] * 52 + [x[i] / x[i - 52] - 1.0 for i in range(52, len(x))]
    c_fx, c_br = chg52(fx), chg52(br)

    series_meta = {}
    for key, ser, chg in (("usdinr", fx, c_fx), ("brent", br, c_br)):
        now_chg = chg[-1]
        pct = pctile_rank(chg[52:], now_chg)
        series_meta[key] = {
            "sym": SERIES[key][0], "definition": SERIES[key][1], "source": SERIES[key][2],
            "last": round(ser[-1], 4), "lastDate": dates[-1], "lastDailyBar": last_bar_on_or_before(last_days[key], dates[-1]),
            "chg12m": round(now_chg * 100, 2),
            "chg12mPctile": pct,
            "state": state_for(pct),
            "basis": f"percentile of 12M change vs {len(chg) - 52} rolling 52-week changes",
        }
    series_meta["nifty"] = {
        "sym": SERIES["nifty"][0], "definition": SERIES["nifty"][1], "source": SERIES["nifty"][2],
        "last": round(nf[-1], 2), "lastDate": dates[-1], "lastDailyBar": last_bar_on_or_before(last_days["nifty"], dates[-1]),
        "chg12m": round((nf[-1] / nf[-53] - 1) * 100, 2),
    }

    # Rolling 52-week correlations of weekly returns (series for the chart + latest + full sample).
    W = 52
    roll = []
    for i in range(W, len(r_nf) + 1):
        a = r_nf[i - W:i]
        roll.append([dates[i], r3(corr(a, r_fx[i - W:i])), r3(corr(a, r_br[i - W:i]))])
    full = {
        "usdinr": r3(corr(r_nf, r_fx)),
        "brent": r3(corr(r_nf, r_br)),
        "n": len(r_nf),
        # does last week's move in the macro series line up with THIS week's Nifty move? (lead of 1 week)
        "lead1w_usdinr": r3(corr(r_nf[1:], r_fx[:-1])),
        "lead1w_brent": r3(corr(r_nf[1:], r_br[:-1])),
    }
    corr_block = {
        "window_weeks": W, "full": full,
        "latest": {"usdinr": roll[-1][1], "brent": roll[-1][2], "n": W},
        "rolling": roll[-ROLL_KEEP:],
        "note": "Weekly-return correlations. They describe the same weeks moving together, not one series leading the other; "
                "lead1w_* is the correlation with the macro move one week earlier. null = undefined (too few points or a flat series).",
    }

    out = {
        "status": "ok",
        "fetchedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asOf": dates[-1],
        "rule": "HEADWIND if the 12M change is in the top quarter of its own 10y history, TAILWIND if in the bottom quarter, else NEUTRAL.",
        "series": series_meta,
        "correlation": corr_block,
        "weekly": {"cols": ["date", "usdinr", "brent", "nifty"],
                   "rows": [[d, round(a, 4), round(b, 4), round(c, 2)] for d, a, b, c in zip(dates, fx, br, nf)]},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {out_path} asOf={dates[-1]} weeks={len(dates)} "
          f"usdinr={series_meta['usdinr']['last']} ({series_meta['usdinr']['state']}) "
          f"brent={series_meta['brent']['last']} ({series_meta['brent']['state']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
