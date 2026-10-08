"""Trailing returns for the Global table, on the same anchor rule as the chart.

index.html (glRenderPx + glCut) draws a 1Y/3Y range as:
    last  = date of the last row in data/global_price_history.json byKey[key]
    cut   = the same calendar date y years earlier (JS setUTCFullYear;
            29 Feb in a non-leap target year rolls to 1 Mar)
    base  = the first daily row with date >= cut
    shown change = last close / base close - 1
The table uses exactly that rule for 1y/3y/5y/10y, on the daily price archive.
YTD (no chart range) uses the first daily close dated on or after 1 Jan of the
last row's year, as the weekly table did before.
"""
from __future__ import annotations

from datetime import date

YEARS = {"1y": 1, "3y": 3, "5y": 5, "10y": 10}


def year_cut(last: str, y: int) -> str:
    """JS glCut(last, y): same month/day y years back; Feb 29 -> Mar 1."""
    d = date.fromisoformat(last)
    try:
        return d.replace(year=d.year - y).isoformat()
    except ValueError:  # 29 Feb into a non-leap year
        return date(d.year - y, 3, 1).isoformat()


def _first_at_or_after(rows: list, cut: str):
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if rows[mid][0] < cut:
            lo = mid + 1
        else:
            hi = mid
    return rows[lo] if lo < len(rows) else None


def _fx_at(fx: dict | None, d: str):
    if fx is None:
        return 1.0
    keys = [k for k in fx if k <= d]
    return fx[max(keys)] if keys else None


def chart_returns(rows: list, fx: dict | None = None, usd: bool = False) -> dict:
    """{ytd,1y,3y,5y,10y} in % (1 dp) from daily [date, close] rows.

    A window whose cut is more than 7 days before the first row gets None. usd=True converts both ends with the weekly
    usd-per-local FX series, forward-filled to the row date."""
    rows = [r for r in rows if r and r[1] and r[1] > 0]
    if not rows:
        return {k: None for k in ("ytd", *YEARS)}
    last_d, last_v = rows[-1]
    anchors = {"ytd": f"{last_d[:4]}-01-01", **{k: year_cut(last_d, y) for k, y in YEARS.items()}}
    out = {}
    for k, cut in anchors.items():
        b = _first_at_or_after(rows, cut)
        # History must reach the cut (7-day grace for holidays at the start);
        # otherwise the window would silently be shorter than its label.
        short = k != "ytd" and (date.fromisoformat(rows[0][0]) - date.fromisoformat(cut)).days > 7
        if b is None or short or b[0] == last_d:
            out[k] = None
            continue
        bv, lv = b[1], last_v
        if usd:
            f0, f1 = _fx_at(fx, b[0]), _fx_at(fx, last_d)
            if f0 is None or f1 is None:
                out[k] = None
                continue
            bv, lv = bv * f0, lv * f1
        out[k] = round((lv / bv - 1.0) * 100, 1)
    return out
