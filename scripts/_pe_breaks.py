"""P/E basis breaks and outliers for data/global_pe_history.json.

A *break* marks a date from which a P/E series is on a different earnings basis
than before it (for worldperatio, a one-step refresh of the trailing-earnings
input: implied earnings E = price / P/E jumps and holds the new level while the
price does not move). Statistics that rank today's P/E against its own history
use only the segment that starts at the latest break.

An *outlier* is a single bad point that reverts (the level before and after it
agree). It stays in the archive but is excluded from statistics.

Both live in the per-key manifest and are never removed by the fetch:
    manifest[key]["breaks"]   = [{"date", "kind": "basis", "source": "manual"|"auto",
                                  "peBefore", "peAfter", "peMove", "priceMove",
                                  "epsMove", "prevDate", "note"}]
    manifest[key]["outliers"] = [{"date", "value", "source", "note", ...}]

Raw points are never deleted or changed here.
"""
from __future__ import annotations

from datetime import date

PE_JUMP = 0.20          # a one-step P/E move above this needs explaining
EXPLAIN_TOL = 0.10      # explained if implied earnings moved by at most this
MAX_GAP_DAYS = 7        # "one-day" check: consecutive points at most this far apart
MIN_SEGMENT = 60        # points needed on the current basis to rank against it
REVERT_TOL = 0.10       # a jump that returns within this of the pre-break level reverts

# P/E key -> price key in global_price_history.json
PRICE_KEY = {"niftyNSE": "nifty"}

INSUFFICIENT_TEXT = "insufficient history on current basis"


def price_key(key: str) -> str:
    return PRICE_KEY.get(key, key)


def close_on_or_before(series: list, d: str):
    """Last [date, close] at or before d, or None. series is date-sorted."""
    lo, hi, best = 0, len(series) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= d:
            best, lo = series[mid], mid + 1
        else:
            hi = mid - 1
    return best


def step(prev: list, cur: list, prices: list | None) -> dict:
    """Describe the move from prev=[d0,pe0] to cur=[d1,pe1] against the price series."""
    (d0, pe0), (d1, pe1) = prev, cur
    pe_move = pe1 / pe0 - 1
    out = {"prevDate": d0, "date": d1, "peBefore": pe0, "peAfter": pe1,
           "peMove": round(pe_move, 4), "priceMove": None, "epsMove": None,
           "gapDays": (date.fromisoformat(d1) - date.fromisoformat(d0)).days}
    if prices:
        a, b = close_on_or_before(prices, d0), close_on_or_before(prices, d1)
        if a and b and a[1] > 0:
            pm = b[1] / a[1] - 1
            out["priceMove"] = round(pm, 4)
            out["priceDates"] = [a[0], b[0]]
            # implied earnings E = P / PE; E1/E0 = (1+pm)/(1+pe_move)
            out["epsMove"] = round((1 + pm) / (1 + pe_move) - 1, 4)
    out["jump"] = abs(pe_move) > PE_JUMP
    out["explained"] = out["epsMove"] is not None and abs(out["epsMove"]) <= EXPLAIN_TOL
    return out


def unexplained_jumps(ser: list, prices: list | None) -> list[dict]:
    """Every consecutive pair whose P/E moved > PE_JUMP and price does not explain it."""
    out = []
    for prev, cur in zip(ser, ser[1:]):
        if not prev[1] or not cur[1]:
            continue
        s = step(prev, cur, prices)
        if s["jump"] and not s["explained"]:
            out.append(s)
    return out


def marked_dates(m: dict) -> tuple[set, set]:
    return ({b["date"] for b in m.get("breaks", [])},
            {o["date"] for o in m.get("outliers", [])})


def is_registered(s: dict, m: dict) -> bool:
    """A jump is accounted for if its date is a break, or either end is an outlier."""
    brk, outl = marked_dates(m)
    return s["date"] in brk or s["date"] in outl or s["prevDate"] in outl


def clean(ser: list, m: dict) -> list:
    """Series without outlier points (raw archive untouched)."""
    _brk, outl = marked_dates(m)
    return [p for p in ser if p[0] not in outl]


def current_segment(ser: list, m: dict) -> tuple[list, str | None]:
    """(points on the current basis, latest break date or None), outliers excluded."""
    pts = clean(ser, m)
    brk = sorted(marked_dates(m)[0])
    if not brk:
        return pts, None
    last = brk[-1]
    return [p for p in pts if p[0] >= last], last


def register_new_point(key: str, ser: list, m: dict, today: str,
                       prices: list | None, log=print) -> None:
    """Called after today's point is merged. Registers an automatic break when
    today's P/E jumped > PE_JUMP with no matching price move; if instead today's
    point reverses an automatic break registered on the previous point, that
    previous point is re-marked as an outlier. Idempotent for a given day."""
    pts = clean(ser, m)
    if len(pts) < 2 or pts[-1][0] != today:
        return
    prev, cur = pts[-2], pts[-1]
    s = step(prev, cur, prices)
    if s["gapDays"] > MAX_GAP_DAYS:
        return
    breaks = m.setdefault("breaks", [])
    # Reversal: yesterday was auto-registered as a break and today is back near
    # the level before it -> yesterday was a one-point glitch, not a new basis.
    last_auto = next((b for b in reversed(breaks)
                      if b.get("source") == "auto" and b["date"] == prev[0]), None)
    if last_auto and abs(cur[1] / last_auto["peBefore"] - 1) <= REVERT_TOL:
        breaks.remove(last_auto)
        m.setdefault("outliers", []).append({
            "date": prev[0], "value": prev[1], "source": "auto",
            "note": f"auto break of {prev[0]} reverted on {today} "
                    f"({last_auto['peBefore']} -> {prev[1]} -> {cur[1]}); re-marked as outlier"})
        log(f"  PE BREAK REVERTED {key}: {prev[0]} re-marked as outlier "
            f"({last_auto['peBefore']} -> {prev[1]} -> {cur[1]})")
        return
    # A same-day revision (3 crons a day) replaces today's break record.
    breaks[:] = [b for b in breaks if not (b.get("source") == "auto" and b["date"] == today)]
    if s["jump"] and not s["explained"]:
        rec = {k: s[k] for k in ("date", "prevDate", "peBefore", "peAfter",
                                 "peMove", "priceMove", "epsMove")}
        rec.update(kind="basis", source="auto",
                   note=("P/E moved %+.1f%% in one step; price %s; implied earnings %s. "
                         "Registered automatically by fetch_global_val.py."
                         % (s["peMove"] * 100,
                            "unavailable" if s["priceMove"] is None else "%+.1f%%" % (s["priceMove"] * 100),
                            "unavailable" if s["epsMove"] is None else "%+.1f%%" % (s["epsMove"] * 100))))
        breaks.append(rec)
        breaks.sort(key=lambda b: b["date"])
        log(f"  PE BREAK REGISTERED {key} {today}: {prev[1]} -> {cur[1]} "
            f"(P/E {s['peMove']:+.1%}, price "
            f"{'n/a' if s['priceMove'] is None else format(s['priceMove'], '+.1%')}, "
            f"implied E {'n/a' if s['epsMove'] is None else format(s['epsMove'], '+.1%')})")
