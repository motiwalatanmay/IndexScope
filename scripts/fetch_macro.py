#!/usr/bin/env python3
"""Fetch macro series (USD/INR, Brent, Nifty) via yfinance -> data/macro.json.

Series (weekly, Friday-anchored, ~11y; the in-progress week is dated today IST):
  usdinr  INR=X   rupees per US dollar (a RISE means a weaker rupee)
  brent   FRED DCOILBRENTEU  Brent SPOT (Europe Brent, EIA via FRED) in USD/bbl.
                  This is the headline Brent. EIA publishes it weekly, so the newest bar is often 3 to 5 business days old.
  brentfut BZ=F   Brent front-month futures, kept as context only. In Sept-Oct 2026
                  it traded 3-25% below the spot series; it feeds no state.
  nifty   ^NSEI   Nifty 50 price index in INR

Partial week: when the newest weekly row is the in-progress week (its Friday has
not closed yet in UTC), the row stays in `weekly.rows` and drives `last` and
`chg12m`, but the file marks it with `partialWeek: true` and `lastCompleteWeek`.
Every correlation (rolling, full, lead1w, latest) and every percentile/state
(`chg12mPctile`, `state`) is computed from COMPLETE weeks only, so a mid-week
quote never moves a correlation or flips a verdict. `chg12mComplete` is the 12M
change at `lastCompleteWeek` (the value that was ranked).

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
import csv
import io
import json
import math
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

DATA = Path(__file__).resolve().parent.parent / "data" / "macro.json"
IST = timezone(timedelta(hours=5, minutes=30))
MIN_WEEKS = 450          # ~8.6y; below this the percentile and correlation are not meaningful
MAX_STALE_DAYS = 8       # newest daily bar older than this = Yahoo gap, refuse to write
ROLL_KEEP = 10 * 52      # rolling-correlation points kept: 10 years of weekly windows
FRED_TRIES = 3
FALLBACK_MAX_DAYS = 21   # a FRED failure may reuse the last committed Brent spot for at most this many days

SERIES = {
    "usdinr": ("INR=X", "Rupees per US dollar (rise = weaker rupee)", "yfinance INR=X"),
    "brent": ("DCOILBRENTEU", "Brent spot (Europe Brent), USD per barrel. Daily, but EIA publishes weekly, so the newest bar is often 3 to 5 business days old.",
              "FRED, else EIA directly"),
    "brentfut": ("BZ=F", "Brent front-month futures (BZ=F), USD per barrel. Context only: it can sit well below spot.",
                 "yfinance BZ=F"),
    "nifty": ("^NSEI", "Nifty 50 price index, INR", "yfinance ^NSEI"),
}


def fred_daily(sid: str):
    """Daily FRED series as a pandas Series (missing '.' rows dropped)."""
    import pandas as pd
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd=2012-01-01"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(1, FRED_TRIES + 1):   # FRED times out now and then from the Actions runner
        try:
            txt = urllib.request.urlopen(req, timeout=45).read().decode()
            break
        except Exception as e:  # noqa: BLE001
            if attempt == FRED_TRIES:
                raise
            print(f"WARN FRED {sid} attempt {attempt}/{FRED_TRIES}: {e}", file=sys.stderr)
            time.sleep(5 * attempt)
    rows = [(r[0], float(r[1])) for r in list(csv.reader(io.StringIO(txt)))[1:] if len(r) == 2 and r[1] not in ("", ".")]
    return pd.Series([v for _, v in rows], index=pd.to_datetime([d for d, _ in rows]))


def prior_brent(path: Path):
    """Last committed Brent spot weekly values and its last daily bar, or (None, None).

    Used only when the FRED fetch fails: USD/INR, futures and Nifty still refresh, and the Brent chip
    keeps its own old `lastDailyBar`, so the page says STALE in words instead of freezing every chip.
    """
    try:
        d = json.loads(path.read_text())
        cols = d["weekly"]["cols"]
        i, j = cols.index("date"), cols.index("brent")
        wk = {r[i]: float(r[j]) for r in d["weekly"]["rows"]}
        bar = d["series"]["brent"]["lastDailyBar"]
        age = (datetime.now(timezone.utc).date() - datetime.strptime(bar, "%Y-%m-%d").date()).days
    except Exception:  # noqa: BLE001
        return None, None
    return (wk, bar) if wk and age <= FALLBACK_MAX_DAYS else (None, None)


def eia_brent_daily():
    """Same Brent spot series straight from EIA (FRED's upstream), as a pandas Series.

    Second source for the Actions runner, where fred.stlouisfed.org times out. Uses EIA_API_KEY when set,
    else the public DEMO_KEY (rate limited, enough for three runs a day).
    """
    import os
    import pandas as pd
    key = os.environ.get("EIA_API_KEY") or "DEMO_KEY"
    url = ("https://api.eia.gov/v2/petroleum/pri/spt/data/?frequency=daily&data%5B0%5D=value"
           "&facets%5Bseries%5D%5B%5D=RBRTE&start=2012-01-01&length=5000"
           "&sort%5B0%5D%5Bcolumn%5D=period&sort%5B0%5D%5Bdirection%5D=asc&api_key=" + key)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    rows = json.loads(urllib.request.urlopen(req, timeout=45).read().decode())["response"]["data"]
    rows = [(r["period"], float(r["value"])) for r in rows if r.get("value") not in (None, "")]
    return pd.Series([v for _, v in rows], index=pd.to_datetime([d for d, _ in rows]))


def brent_spot():
    try:
        return fred_daily("DCOILBRENTEU")
    except Exception as e:  # noqa: BLE001
        print(f"WARN FRED failed ({e}); trying EIA", file=sys.stderr)
        return eia_brent_daily()


def pull(sym: str):
    if sym == "DCOILBRENTEU":
        s = brent_spot()
    else:
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


def week_is_partial(label: str, today_utc) -> bool:
    """True when the W-FRI week holding `label` has not closed yet.

    The week closes at the end of its Friday; Brent (the last of the three to
    settle) closes ~22:00 UTC Friday, so the week counts as complete from the
    next UTC day. `label` may be a remapped in-progress date (today IST), so the
    Friday anchor is computed rather than assumed.
    """
    d = datetime.strptime(label, "%Y-%m-%d").date()
    friday = d + timedelta(days=(4 - d.weekday()) % 7)
    return friday >= today_utc


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
    raw, last_days, fallback = {}, {}, set()
    for key, (sym, _d, _s) in SERIES.items():
        try:
            raw[key], last_days[key] = pull(sym)
        except Exception as e:  # noqa: BLE001
            wk, bar = prior_brent(out_path) if key == "brent" else (None, None)
            if wk is None:
                print(f"FAIL {key} {sym}: {e}", file=sys.stderr)
                return 1
            print(f"WARN {key} {sym}: {e}; reusing the last committed values (last bar {bar})", file=sys.stderr)
            raw[key], last_days[key] = dict(wk), [bar]
            fallback.add(key)
            continue
        if not raw[key] or len(raw[key]) < MIN_WEEKS:
            print(f"FAIL {key}: thin series ({len(raw[key] or {})} weeks)", file=sys.stderr)
            return 1
        age = (now.date() - datetime.strptime(last_days[key][-1], "%Y-%m-%d").date()).days
        if age > MAX_STALE_DAYS:
            print(f"FAIL {key}: newest bar {last_days[key][-1]} is {age} days old", file=sys.stderr)
            return 1

    if raw["brent"]:
        # EIA publishes Brent spot weekly, so FRED always trails the other series. Carry its last value into the
        # weeks it has not reached; without this the whole file (USD/INR, Nifty) stops at the last Brent week.
        # The Brent chip keeps its own `lastDailyBar`, so the page still says STALE in words.
        last_wk = max(raw["brent"])
        for d in sorted(set(raw["usdinr"]) & set(raw["nifty"]) & set(raw["brentfut"])):
            if d > last_wk:
                raw["brent"][d] = raw["brent"][last_wk]
    common = sorted(set(raw["usdinr"]) & set(raw["brent"]) & set(raw["nifty"]) & set(raw["brentfut"]))
    # A partial (in-progress) newest week is kept for display but excluded from every statistic.
    # The window holds 572 COMPLETE weeks either way, so n / basis counts do not wobble mid-week.
    partial = bool(common) and week_is_partial(common[-1], now.date())
    dates = common[-(572 + int(partial)):]
    if len(dates) - int(partial) < MIN_WEEKS:
        print(f"FAIL: only {len(dates) - int(partial)} complete common weeks", file=sys.stderr)
        return 1
    nc = len(dates) - int(partial)          # number of complete weeks; dates[:nc] are complete
    last_complete = dates[nc - 1]
    fx = [raw["usdinr"][d] for d in dates]
    br = [raw["brent"][d] for d in dates]
    nf = [raw["nifty"][d] for d in dates]
    bf = [raw["brentfut"][d] for d in dates]
    dc = dates[:nc]
    fxc, brc, nfc = fx[:nc], br[:nc], nf[:nc]

    def rets(x):
        return [math.log(x[i] / x[i - 1]) for i in range(1, len(x))]
    r_fx, r_br, r_nf = rets(fxc), rets(brc), rets(nfc)   # complete weeks only; r[i] is the return into dc[i+1]

    # 12M change series (52 weeks). Chips show the change at the last quote; the
    # percentile/state rank the last COMPLETE week's change against complete-week history.
    def chg52(x):
        return [None] * 52 + [x[i] / x[i - 52] - 1.0 for i in range(52, len(x))]
    c_fx, c_br = chg52(fx), chg52(br)

    series_meta = {}
    for key, ser, chg in (("usdinr", fx, c_fx), ("brent", br, c_br)):
        now_chg = chg[-1]
        hist = chg[52:nc]
        pct = pctile_rank(hist, chg[nc - 1])
        series_meta[key] = {
            "sym": SERIES[key][0], "definition": SERIES[key][1], "source": SERIES[key][2],
            "last": round(ser[-1], 4), "lastDate": dates[-1], "lastDailyBar": last_bar_on_or_before(last_days[key], dates[-1]),
            "chg12m": round(now_chg * 100, 2),
            "chg12mComplete": round(chg[nc - 1] * 100, 2),
            "chg12mPctile": pct,
            "state": state_for(pct),
            **({"fallback": "FRED fetch failed; Brent spot is the last committed value"} if key in fallback else {}),
            "basis": f"percentile of the 12M change at the last complete week ({last_complete}) "
                     f"vs {len(hist)} rolling 52-week changes (complete weeks only)",
        }
    series_meta["brentfut"] = {
        "sym": SERIES["brentfut"][0], "definition": SERIES["brentfut"][1], "source": SERIES["brentfut"][2],
        "last": round(bf[-1], 4), "lastDate": dates[-1], "lastDailyBar": last_bar_on_or_before(last_days["brentfut"], dates[-1]),
        "chg12m": round((bf[-1] / bf[-53] - 1) * 100, 2),
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
        roll.append([dc[i], r3(corr(a, r_fx[i - W:i])), r3(corr(a, r_br[i - W:i]))])
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
        "asOf": last_complete,
        "note": "Weekly-return correlations over complete weeks only (an in-progress week is excluded). They describe the same "
                "weeks moving together, not one series leading the other; lead1w_* is the correlation with the macro move one "
                "week earlier. null = undefined (too few points or a flat series).",
    }

    out = {
        "status": "ok",
        "fetchedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asOf": dates[-1],
        "partialWeek": partial,
        "lastCompleteWeek": last_complete,
        "rule": "HEADWIND if the 12M change is in the top quarter of its own 10y history, TAILWIND if in the bottom quarter, else NEUTRAL.",
        "series": series_meta,
        "correlation": corr_block,
        "weekly": {"cols": ["date", "usdinr", "brent", "nifty", "brentfut"],
                   "rows": [[d, round(a, 4), round(b, 4), round(c, 2), round(e, 4)] for d, a, b, c, e in zip(dates, fx, br, nf, bf)]},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {out_path} asOf={dates[-1]} weeks={len(dates)} partialWeek={partial} lastCompleteWeek={last_complete} "
          f"usdinr={series_meta['usdinr']['last']} ({series_meta['usdinr']['state']}) "
          f"brent={series_meta['brent']['last']} ({series_meta['brent']['state']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
