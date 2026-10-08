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

P/E history (data/global_pe_history.json) is an append-only archive:
  - byKey.<key>     worldperatio country-basket P/E (Wayback seed + live scrape)
  - byKey.niftyNSE  NSE Nifty 50 trailing P/E (seeded from the embedded
                    N50_HISTORY, extended from data/n50.json every run)
  - manifest.<key>  definition / source / firstDate / method per series
A run never removes a point, never rewrites a point older than REVISE_DAYS,
writes atomically, and refuses to write if any series would shrink. An
unreadable history file aborts the run instead of being replaced.
global_val.json gains `bandStats` (distribution of each P/E series) and
per-index `pctile` / `n`; the verdict rule is unchanged.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from _archive_io import (ArchiveError, assert_no_loss, atomic_write_json,
                         load_json_strict, merge_series)
import _pe_breaks as PB

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


DEFINITIONS = {
    "nifty":  "worldperatio India country basket trailing P/E (NOT the Nifty 50)",
    "niftyNSE": "NSE Nifty 50 trailing P/E as published by NSE / niftyindices",
    "spx":    "worldperatio United States country basket trailing P/E",
    "csi300": "worldperatio China country basket trailing P/E",
    "hsi":    "worldperatio Hong Kong country basket trailing P/E",
    "nikkei": "worldperatio Japan country basket trailing P/E",
    "kospi":  "worldperatio South Korea country basket trailing P/E",
    "taiex":  "worldperatio Taiwan country basket trailing P/E",
    "ftse":   "worldperatio United Kingdom country basket trailing P/E",
    "estoxx": "mean of worldperatio Germany and France basket trailing P/E (euro proxy)",
}


def refresh_manifest(doc: dict) -> None:
    """Recompute per-series provenance. Keeps any static fields already there."""
    man = doc.setdefault("manifest", {})
    for key, ser in doc.get("byKey", {}).items():
        m = man.setdefault(key, {})
        m["definition"] = DEFINITIONS.get(key, m.get("definition", "unknown"))
        m["firstDate"] = ser[0][0] if ser else None
        m["lastDate"] = ser[-1][0] if ser else None
        m["n"] = len(ser)
        if key == "niftyNSE":
            m.setdefault("source", "NSE Nifty 50 P/E: embedded N50_HISTORY (niftyindices) + data/n50.json (NSE /api/allIndices)")
            seed_to = (m.get("seed") or {}).get("to", "")
            live = [d for d, _ in ser if d > seed_to]
            m["method"] = {"snapshot": "historical daily series (seed)",
                           "seed": m.get("seed"),
                           "live": {"from": live[0] if live else None, "n": len(live),
                                    "source": "data/n50.json, daily NSE scrape"}}
            continue
        # liveFrom = start of the daily live-scrape run (first computed from the
        # trailing gap-free tail, then frozen). Points before it are Wayback
        # snapshots; points from it on are live scrapes (a later Wayback merge
        # never overwrites a live point, see seed_global_pe_history.py).
        if "liveFrom" not in m and ser:
            ds = [date.fromisoformat(d) for d, _ in ser]
            i = len(ds) - 1
            while i > 0 and (ds[i] - ds[i - 1]).days <= 3:
                i -= 1
            m["liveFrom"] = ds[i].isoformat()
        live_from = m.get("liveFrom", "9999")
        s_dates = sorted(d for d, _ in ser if d < live_from)
        l_dates = sorted(d for d, _ in ser if d >= live_from)
        m["source"] = "worldperatio.com"
        m["method"] = {
            "snapshot": {"source": "web.archive.org snapshots of worldperatio.com",
                         "from": s_dates[0] if s_dates else None,
                         "to": s_dates[-1] if s_dates else None, "n": len(s_dates)},
            "live": {"source": "worldperatio.com scrape by fetch_global_val.py",
                     "from": l_dates[0] if l_dates else None,
                     "to": l_dates[-1] if l_dates else None, "n": len(l_dates)},
        }


KEY_COUNTRY = {k: c for k, (c, _lo, _hi) in MAP.items()}


def _q(vals: list, p: float) -> float:
    v = sorted(vals)
    k = (len(v) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return round(v[f] + (v[c] - v[f]) * (k - f), 2)


def pctile_rank(vals: list, cur: float) -> float | None:
    if not vals or cur is None:
        return None
    lt = sum(1 for v in vals if v < cur)
    eq = sum(1 for v in vals if v == cur)
    return round((lt + 0.5 * eq) / len(vals) * 100, 1)


def series_stats(ser: list, today: str, man: dict | None = None) -> dict:
    """Distribution blocks for one P/E series. Outliers (manifest) are excluded.
    'all' = full history; 'current' = segment since the latest basis break, only
    when it has >= PB.MIN_SEGMENT points; 'basis' says which one to rank against.
    A block that spans a break carries spansBreak=true: its percentile mixes two
    earnings bases and must not be read as a valuation rank."""
    man = man or {}
    pts = PB.clean(ser, man)
    seg, brk = PB.current_segment(ser, man)

    def block(p):
        vals = [v for _, v in p]
        b = {"start": p[0][0], "end": p[-1][0], "n": len(vals),
             "min": min(vals), "p10": _q(vals, 10), "p25": _q(vals, 25),
             "median": _q(vals, 50), "p75": _q(vals, 75), "p90": _q(vals, 90),
             "max": max(vals), "current": vals[-1],
             "pctile": pctile_rank(vals, vals[-1])}
        if brk and p[0][0] < brk:
            b["spansBreak"] = True
        return b
    out = {"all": block(pts)}
    ten = f"{int(today[:4]) - 10}{today[4:]}"
    if pts[0][0] < ten:
        recent = [p for p in pts if p[0] >= ten]
        if recent:
            out["10y"] = block(recent)
    ok = brk is None or len(seg) >= PB.MIN_SEGMENT
    if brk and ok:
        out["current"] = block(seg)
    out["basis"] = {
        "breakDate": brk, "n": len(seg), "minPoints": PB.MIN_SEGMENT,
        "sufficient": ok, "rankWindow": ("current" if brk and ok else "all" if ok else None),
        "outliersExcluded": sorted(PB.marked_dates(man)[1]),
        "breaks": [b["date"] for b in man.get("breaks", [])],
    }
    if not ok:
        out["basis"]["text"] = PB.INSUFFICIENT_TEXT
    return out


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
    # Append today's worldperatio reading to the append-only P/E archive
    # (de-duped by date), then band off the UPDATED history.
    today = now_utc.astimezone(IST).strftime("%Y-%m-%d")
    hist_path = DATA_DIR / "global_pe_history.json"
    try:
        hist_doc = load_json_strict(hist_path, default=None)
    except ArchiveError as e:
        print(f"FATAL: {e}; refusing to overwrite the P/E archive", file=sys.stderr)
        return 1
    if hist_doc is None:
        hist_doc = {"source": "worldperatio.com (live-extended)",
                    "metric": "trailing P/E (country basket)", "byKey": {}}
    by_key = hist_doc.setdefault("byKey", {})
    old_by_key = {k: list(v) for k, v in by_key.items()}
    # worldperatio repeats the last value when it has not refreshed (weekends,
    # holidays). Skip the append when EVERY mapped market equals its previous
    # dated point: that is a stale page, not a new observation.
    today_vals = {}
    for key, (country, _lo, _hi) in MAP.items():
        v = euro if country == "__euro__" else pes.get(country)
        if v is not None:
            today_vals[key] = round(float(v), 2)
    def _prev(key):
        prior = [p for p in by_key.get(key, []) if p[0] < today]
        return prior[-1][1] if prior else None
    stale = bool(today_vals) and all(_prev(k) == v for k, v in today_vals.items())
    if stale:
        print(f"  worldperatio unchanged vs previous point for all markets; "
              f"no {today} point appended (stale page)")
    else:
        for key, v in today_vals.items():
            by_key[key], _ = merge_series(by_key.get(key, []), [[today, v]], today)
        # A > 20% one-step P/E move with no matching price move is a basis
        # break (or, if it reverts next point, an outlier): register it so the
        # stats below rank only against the current basis. Logged, never silent.
        try:
            px_by_key = (load_json_strict(DATA_DIR / "global_price_history.json", default={})
                         or {}).get("byKey", {})
        except ArchiveError as e:
            px_by_key = {}
            print(f"WARN: price archive unreadable ({e}); P/E jumps judged without prices",
                  file=sys.stderr)
        man_all = hist_doc.setdefault("manifest", {})
        for key in today_vals:
            PB.register_new_point(key, by_key[key], man_all.setdefault(key, {}), today,
                                  px_by_key.get(PB.price_key(key)))
    # NSE Nifty 50 P/E: fold in every n50.json row (date-deduped).
    try:
        n50_rows = json.loads((DATA_DIR / "n50.json").read_text()).get("data", [])
        nse_pts = [[r[0], round(float(r[2]), 2)] for r in n50_rows if len(r) > 2 and r[2]]
        by_key["niftyNSE"], st = merge_series(by_key.get("niftyNSE", []), nse_pts, today)
        print(f"  niftyNSE: +{st['added']} new, {st['revised']} revised from n50.json")
    except Exception as e:  # noqa: BLE001
        print(f"WARN: niftyNSE extend from n50.json failed: {e}", file=sys.stderr)
    refresh_manifest(hist_doc)
    hist_doc["lastUpdated"] = today
    try:
        assert_no_loss(old_by_key, by_key, "global_pe_history.json")
    except ArchiveError as e:
        print(f"FATAL: {e}; P/E archive left untouched", file=sys.stderr)
        return 1
    atomic_write_json(hist_path, hist_doc)
    print(f"  global_pe_history.json: " + ", ".join(f"{k}={len(v)}" for k, v in by_key.items()))

    hist = by_key
    man_all = hist_doc.get("manifest", {})
    for rec in out:
        if rec["key"] == "nifty":
            rec["bandNote"] = "long-run ref"
            continue
        man = man_all.get(rec["key"], {})
        seg, brk = PB.current_segment(hist.get(rec["key"], []), man)
        vals = [v for _, v in seg]
        if brk and len(vals) < PB.MIN_SEGMENT:
            # The series changed earnings basis recently: ranking today's P/E
            # against the old basis would mislead, and the new basis is too short.
            rec["verdict"] = "insufficient"
            rec["verdictText"] = PB.INSUFFICIENT_TEXT
            rec["bandNote"] = (f"{PB.INSUFFICIENT_TEXT}: {len(vals)} of {PB.MIN_SEGMENT} points "
                               f"since the {brk} break; long-run ref")
            rec["basisFrom"] = brk
        elif len(vals) >= 8:
            lo, hi = round(_pctl(vals, 25), 1), round(_pctl(vals, 75), 1)
            if hi - lo < 3:                       # floor the spread so it isn't hair-trigger
                mid = (lo + hi) / 2
                lo, hi = round(mid - 1.5, 1), round(mid + 1.5, 1)
            rec["refLo"], rec["refHi"] = lo, hi
            rec["verdict"] = verdict(rec["pe"], lo, hi)
            rec["bandNote"] = (f"own range since {seg[0][0]} (basis break)" if brk
                               else f"own range since {seg[0][0][:4]}")
            if brk:
                rec["basisFrom"] = brk
        else:
            rec["bandNote"] = "long-run ref"

    # Distribution of each P/E series from its own history (not static),
    # ranked on the current earnings basis only.
    band_stats = {k: series_stats(v, today, man_all.get(k)) for k, v in by_key.items() if v}
    for rec in out:
        sk = "niftyNSE" if rec["key"] == "nifty" else rec["key"]
        bs = band_stats.get(sk)
        if bs:
            basis = bs["basis"]
            win = basis["rankWindow"]
            seg, _brk = PB.current_segment(by_key[sk], man_all.get(sk, {}))
            rec["pctile"] = pctile_rank([v for _, v in seg], rec["pe"]) if win else None
            rec["n"] = basis["n"]
            rec["pctileSeries"] = sk
            rec["pctileFrom"] = seg[0][0] if seg else None
            rec["pctileWindow"] = win

    doc = {
        "status": "ok",
        "fetchedAt": now_utc.isoformat().replace("+00:00", "Z"),
        "asOf": now_utc.astimezone(IST).strftime("%Y-%m-%d"),
        "source": "worldperatio.com country baskets (trailing P/E) · India=NSE Nifty 50 · US CAPE=multpl.com",
        "note": "Trailing P/E. Non-India figures use worldperatio's consistent "
                "country-basket methodology (operating earnings) for cross-country "
                "comparability — these are broad baskets and can differ from a single "
                "headline index. India uses NSE Nifty 50 to match the Dashboard. "
                "Non-India bands are the p25-p75 of each market's own P/E history, which "
                "extends daily; India uses a static long-run reference band.",
        "indices": out,
        "bandStats": band_stats,
        "bandStatsNote": "Per P/E series in global_pe_history.json: min/max/median/"
                         "p10/p25/p75/p90 over the full history ('all') and the "
                         "last 10y where history is longer. pctile = share of "
                         "history at or below the latest value (ties half). "
                         "Indices.pctile for nifty uses niftyNSE (the NSE Nifty 50 "
                         "series the card shows). Outliers listed in the archive "
                         "manifest are excluded. When a series has a basis break "
                         "(manifest breaks: a one-step earnings-input change, not a "
                         "market move), indices.pctile/n and the band use only the "
                         "points since the latest break ('current' block); with "
                         f"fewer than {PB.MIN_SEGMENT} such points the verdict is "
                         f"'insufficient' ({PB.INSUFFICIENT_TEXT}) and pctile is null. "
                         "Blocks with spansBreak=true mix two bases.",
    }
    atomic_write_json(DATA_DIR / "global_val.json", doc)
    print(f"\nwrote data/global_val.json  ({len(out)} indices)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
