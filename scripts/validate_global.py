#!/usr/bin/env python3
"""Validate the global-index archive before it is committed. Exit 1 on failure.

Checks
  global_pe_history.json   per series: valid ISO dates, strictly increasing
                           (monotone, no duplicates), 0 < P/E <= 100; every
                           series has a manifest entry with definition,
                           source, firstDate and method.
  global_price_history.json per series: same date rules, close > 0, and any
                           day-over-day move > 40% must be listed in flags.
  global.json              each weekly series monotone with no duplicates.
  global_val.json          every pe in (0, 100].
  floors                   no series has fewer points than the stored minimum
                           in data/global_archive_floor.json.
  --against-git REF        every (series, date) present at REF is still present
                           (proves no point was lost), and archived values older
                           than the revise window are unchanged.

--update-floor raises the stored minimums to the current counts after all
checks pass (never lowers them).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FLOOR = DATA / "global_archive_floor.json"
REVISE_DAYS = 10
JUMP = 0.40
PE_FILE, PX_FILE = "global_pe_history.json", "global_price_history.json"

errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def load(name: str):
    p = DATA / name
    if not p.exists():
        err(f"{name}: missing")
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        err(f"{name}: unparseable ({e})")
        return None


def check_dates(label: str, ser: list) -> None:
    prev = None
    for row in ser:
        d = row[0]
        try:
            date.fromisoformat(d)
        except Exception:  # noqa: BLE001
            err(f"{label}: bad date {d!r}")
            return
        if prev is not None and d <= prev:
            err(f"{label}: dates not strictly increasing at {prev} -> {d}"
                + (" (duplicate)" if d == prev else ""))
            return
        prev = d


def check_pe(doc: dict) -> None:
    man = doc.get("manifest", {})
    for key, ser in doc.get("byKey", {}).items():
        lab = f"{PE_FILE}:{key}"
        check_dates(lab, ser)
        bad = [p for p in ser if not isinstance(p[1], (int, float)) or not (0 < p[1] <= 100)]
        if bad:
            err(f"{lab}: P/E out of (0,100]: {bad[:3]}")
        m = man.get(key)
        if not m:
            err(f"{lab}: no manifest entry")
        else:
            miss = [f for f in ("definition", "source", "firstDate", "method") if not m.get(f)]
            if miss:
                err(f"{lab}: manifest missing {miss}")
    for c, ser in doc.get("country", {}).items():
        check_dates(f"{PE_FILE}:country:{c}", ser)


def check_px(doc: dict) -> None:
    flags = doc.get("flags", {})
    for key, ser in doc.get("byKey", {}).items():
        lab = f"{PX_FILE}:{key}"
        check_dates(lab, ser)
        bad = [p for p in ser if not isinstance(p[1], (int, float)) or p[1] <= 0]
        if bad:
            err(f"{lab}: non-positive close {bad[:3]}")
            continue
        flagged = {f[0] for f in flags.get(key, [])}
        for (d0, v0), (d1, v1) in zip(ser, ser[1:]):
            if abs(v1 / v0 - 1) > JUMP and d1 not in flagged:
                err(f"{lab}: unflagged {((v1 / v0) - 1) * 100:+.1f}% move {d0}->{d1}")
        if not doc.get("manifest", {}).get(key):
            err(f"{lab}: no manifest entry")


def counts(pe, px, gl) -> dict:
    return {
        "pe": {k: len(v) for k, v in (pe or {}).get("byKey", {}).items()},
        "price": {k: len(v) for k, v in (px or {}).get("byKey", {}).items()},
        "globalIndices": len((gl or {}).get("indices", [])),
    }


def check_floor(cur: dict) -> dict:
    floor = {}
    if FLOOR.exists():
        try:
            floor = json.loads(FLOOR.read_text())
        except Exception as e:  # noqa: BLE001
            err(f"{FLOOR.name}: unparseable ({e})")
            return {}
    for grp in ("pe", "price"):
        for k, n in floor.get(grp, {}).items():
            have = cur[grp].get(k, 0)
            if have < n:
                err(f"floor: {grp}:{k} has {have} points, stored minimum {n}")
    if cur["globalIndices"] < floor.get("globalIndices", 0):
        err(f"floor: global.json has {cur['globalIndices']} indices, minimum {floor['globalIndices']}")
    return floor


def git_json(ref: str, name: str):
    r = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:data/{name}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None  # file did not exist at ref
    return json.loads(r.stdout)


def check_against(ref: str, cur_docs: dict) -> None:
    today = datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=REVISE_DAYS)).isoformat()
    for name, cur in cur_docs.items():
        old = git_json(ref, name)
        if old is None or cur is None:
            print(f"  {name}: no baseline at {ref}" if old is None else "")
            continue
        lost = changed = 0
        for blk in ("byKey", "country"):
            for key, ser in (old.get(blk) or {}).items():
                new = dict((p[0], p[1]) for p in (cur.get(blk) or {}).get(key, []))
                for d, v in ser:
                    if d not in new:
                        lost += 1
                        err(f"{name}:{blk}:{key}: point {d} present at {ref} is missing")
                    elif new[d] != v and d < cutoff:
                        changed += 1
                        err(f"{name}:{blk}:{key}: frozen point {d} changed {v} -> {new[d]}")
        print(f"  {name}: vs {ref}: lost={lost} frozen-changed={changed}")



# --- P/E basis breaks, verdict basis and table/chart returns (2026-10-08) ---
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _pe_breaks as PB  # noqa: E402
from _returns import chart_returns  # noqa: E402


def check_pe_jumps(pe: dict, px: dict | None) -> None:
    """A P/E one-step move > 20% that price does not explain must be a
    registered break (or touch a registered outlier). Pairs more than
    MAX_GAP_DAYS apart (old snapshot era) are warnings, not failures."""
    pxk = (px or {}).get("byKey", {})
    man = pe.get("manifest", {})
    for key, ser in pe.get("byKey", {}).items():
        m = man.get(key, {})
        dates = {p[0] for p in ser}
        for kind in ("breaks", "outliers"):
            for b in m.get(kind, []):
                if b.get("date") not in dates:
                    err(f"{PE_FILE}:{key}: {kind[:-1]} {b.get('date')} is not a date in the series")
        for s in PB.unexplained_jumps(ser, pxk.get(PB.price_key(key))):
            if PB.is_registered(s, m):
                continue
            msg = (f"{PE_FILE}:{key}: P/E {s['prevDate']} {s['peBefore']} -> {s['date']} {s['peAfter']} "
                   f"({s['peMove']:+.1%}), price "
                   f"{'n/a' if s['priceMove'] is None else format(s['priceMove'], '+.1%')}, "
                   f"implied E {'n/a' if s['epsMove'] is None else format(s['epsMove'], '+.1%')}")
            if s["gapDays"] <= PB.MAX_GAP_DAYS:
                err("unregistered P/E jump: " + msg)
            else:
                print(f"WARN gap {s['gapDays']}d, not checked as a one-day jump: " + msg)


def check_val_basis(val: dict, pe: dict) -> None:
    """Verdicts and percentiles must sit on the current earnings basis."""
    man = pe.get("manifest", {})
    for r in val.get("indices", []):
        key = r.get("key")
        if key == "nifty":
            continue
        ser = pe.get("byKey", {}).get(key)
        if not ser:
            continue
        seg, brk = PB.current_segment(ser, man.get(key, {}))
        short = brk is not None and len(seg) < PB.MIN_SEGMENT
        if short and r.get("verdict") != "insufficient":
            err(f"global_val.json:{key}: {len(seg)} points since the {brk} break, "
                f"verdict must be 'insufficient', got {r.get('verdict')!r}")
        if short and r.get("pctile") is not None:
            err(f"global_val.json:{key}: pctile set on a {len(seg)}-point basis")
        if not short and r.get("verdict") == "insufficient":
            err(f"global_val.json:{key}: verdict 'insufficient' but {len(seg)} points on the current basis")
        if brk and not short and (r.get("pctileFrom") or "") < brk:
            err(f"global_val.json:{key}: pctileFrom {r.get('pctileFrom')} is before the {brk} break")


def check_returns(gl: dict, px: dict) -> None:
    """The returns table must equal the chart's range change: recompute from
    the daily archive with the chart rule (scripts/_returns.py) and compare.
    Windows the archive does not cover yet are skipped."""
    pxk = px.get("byKey", {})
    for r in gl.get("indices", []):
        key, rows = r.get("key"), pxk.get(r.get("key"))
        if r.get("stale") or not rows:
            continue
        if r.get("lastDate") != rows[-1][0]:
            err(f"global.json:{key}: lastDate {r.get('lastDate')} != last archive close {rows[-1][0]}")
        want = chart_returns(rows)
        for k, v in want.items():
            got = (r.get("ret") or {}).get(k)
            if v is not None and (got is None or abs(got - v) > 0.15):
                err(f"global.json:{key}: ret.{k} {got} != chart-rule {v} from the daily archive")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--against-git", metavar="REF")
    ap.add_argument("--update-floor", action="store_true")
    args = ap.parse_args()

    pe, px = load(PE_FILE), load(PX_FILE)
    gl, val = load("global.json"), load("global_val.json")
    if pe:
        check_pe(pe)
    if px:
        check_px(px)
    for r in (gl or {}).get("indices", []):
        check_dates(f"global.json:{r.get('key')}", r.get("series", []))
    for r in (val or {}).get("indices", []):
        if not (0 < (r.get("pe") or 0) <= 100):
            err(f"global_val.json:{r.get('key')}: pe {r.get('pe')} out of (0,100]")
    if pe:
        check_pe_jumps(pe, px)
        if val:
            check_val_basis(val, pe)
    if gl and px:
        check_returns(gl, px)
    cur = counts(pe, px, gl)
    floor = check_floor(cur)
    if args.against_git:
        check_against(args.against_git, {PE_FILE: pe, PX_FILE: px})

    print("counts: pe=" + json.dumps(cur["pe"]) + " price=" + json.dumps(cur["price"])
          + f" globalIndices={cur['globalIndices']}")
    if errors:
        print(f"FAIL: {len(errors)} problem(s)", file=sys.stderr)
        for e in errors[:50]:
            print("  " + e, file=sys.stderr)
        return 1
    if args.update_floor:
        new = {"note": "Minimum points per series; validate_global.py fails below these. "
                       "Raised automatically, never lowered.",
               "pe": {k: max(n, floor.get("pe", {}).get(k, 0)) for k, n in cur["pe"].items()},
               "price": {k: max(n, floor.get("price", {}).get(k, 0)) for k, n in cur["price"].items()},
               "globalIndices": max(cur["globalIndices"], floor.get("globalIndices", 0))}
        for grp in ("pe", "price"):  # keep floors for keys that vanished (they then fail)
            for k, n in floor.get(grp, {}).items():
                new[grp].setdefault(k, n)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _archive_io import atomic_write_json
        atomic_write_json(FLOOR, new)
        print(f"floor updated: {FLOOR.name}")
    print("OK: global archive valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
