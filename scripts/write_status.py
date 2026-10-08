#!/usr/bin/env python3
"""Write data/status.json: a freshness report for every data feed plus the
GitHub Action run that produced it. Read by the admin tab.

Runs near the end of .github/workflows/update-indices.yml with
`if: always()` and `continue-on-error: true`. It must never fail the Action:
every error is caught, recorded in the output, and the script exits 0.

Per feed it records lastDate, fetchedAt, ageHours, ageBusinessDays, rows and
a state word:
  OK       within thresholds
  LATE     past the LATE threshold
  STALE    past the STALE threshold
  MISSING  file absent, unreadable, or carries no date

Thresholds
  Action feeds (kind "bd"): business days (Mon-Fri, IST; NSE holidays are NOT
  modelled) strictly after lastDate up to and including today.
  LATE if > 1, STALE if > 4.
  buffett (kind "hours", source "mac-job"): hours since fetchedAt, counting
  only weekday (IST) hours because the Mac job runs Mon-Fri.
  LATE if > 12, STALE if > 36. ageHours is still the raw wall-clock age.

Run metadata comes from the standard GitHub env (GITHUB_RUN_ID, ...).
Step outcomes come from STEP_<NAME> env vars the workflow passes in
(values: success | failure | cancelled | skipped).

Usage:
  python scripts/write_status.py [--data-dir data] [--out data/status.json]
                                 [--now 2026-10-08T15:00:00Z]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent

STATE_RANK = {"OK": 0, "LATE": 1, "STALE": 2, "MISSING": 3}


def _rows_data(d):
    return len(d.get("data") or [])


def _rows_gsec(d):
    rows = d.get("data") or []
    return max(len(rows) - 1, 0) if rows and rows[0] and rows[0][0] == "Date" else len(rows)


def _rows_pe_hist(d):
    by = d.get("byKey") or {}
    return max((len(v) for v in by.values() if isinstance(v, list)), default=0)


def _buffett_date(d):
    return (d.get("live") or {}).get("date") or ((d.get("daily") or [[None]])[-1] or [None])[0]


# key -> (file, lastDate getter, rows getter, threshold spec, source)
FEEDS = {
    "n50":     ("n50.json",     lambda d: d.get("lastDate"), _rows_data, "bd", "action"),
    "nn50":    ("nn50.json",    lambda d: d.get("lastDate"), _rows_data, "bd", "action"),
    "nmid150": ("nmid150.json", lambda d: d.get("lastDate"), _rows_data, "bd", "action"),
    "sc250":   ("sc250.json",   lambda d: d.get("lastDate"), _rows_data, "bd", "action"),
    "n500":    ("n500.json",    lambda d: d.get("lastDate"), _rows_data, "bd", "action"),
    "gsec":    ("gsec.json",    lambda d: d.get("lastDate"), _rows_gsec, "bd", "action"),
    "global":  ("global.json",  lambda d: d.get("asOf"),
                lambda d: len(d.get("indices") or []), "bd", "action"),
    "global_val": ("global_val.json", lambda d: d.get("asOf"),
                   lambda d: len(d.get("indices") or []), "bd", "action"),
    "global_pe_history": ("global_pe_history.json", lambda d: d.get("lastUpdated"),
                          _rows_pe_hist, "bd", "action"),
    "macro":   ("macro.json",   lambda d: d.get("asOf"),
                lambda d: len((d.get("weekly") or {}).get("rows") or []), "bd", "action"),
    "fii":     ("fii.json",     lambda d: d.get("lastReportDate"),
                lambda d: len(d.get("rows") or []), "bd", "action"),
    "buffett": ("buffett.json", _buffett_date,
                lambda d: len(d.get("daily") or []), "hours", "mac-job"),
}

THRESHOLDS = {
    "bd":    {"unit": "businessDays", "late": 1, "stale": 4},
    "hours": {"unit": "weekdayHours", "late": 12, "stale": 36},
}

# Workflow step ids -> env var carrying that step's outcome.
STEPS = ["indices", "global", "global_val", "macro", "fii", "gsec", "alerts"]
REQUIRED_STEPS = {"indices"}  # the only step without continue-on-error


def parse_ts(s):
    if not s or not isinstance(s, str):
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def parse_date(s):
    if not s or not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def business_days_after(last: date, today: date) -> int:
    """Weekdays d with last < d <= today. 0 when last >= today."""
    if last >= today:
        return 0
    n, d = 0, last + timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def weekday_hours_between(start: datetime, end: datetime) -> float:
    """Hours in [start, end) that fall on Mon-Fri in IST."""
    if end <= start:
        return 0.0
    s, e = start.astimezone(IST), end.astimezone(IST)
    total, cur = 0.0, s
    while cur < e:
        nxt = min(e, datetime.combine(cur.date() + timedelta(days=1),
                                      datetime.min.time(), IST))
        if cur.weekday() < 5:
            total += (nxt - cur).total_seconds() / 3600
        cur = nxt
    return total


def grade(value, spec):
    if value is None:
        return "MISSING"
    if value > spec["stale"]:
        return "STALE"
    if value > spec["late"]:
        return "LATE"
    return "OK"


def feed_status(key, data_dir: Path, now: datetime):
    fname, get_date, get_rows, kind, source = FEEDS[key]
    spec = THRESHOLDS[kind]
    out = {"key": key, "file": f"data/{fname}", "source": source,
           "lastDate": None, "fetchedAt": None, "ageHours": None,
           "ageBusinessDays": None, "rows": None, "fileStatus": None,
           "state": "MISSING",
           "thresholds": {"unit": spec["unit"], "late": spec["late"], "stale": spec["stale"]}}
    path = data_dir / fname
    if not path.exists():
        out["error"] = "file not found"
        return out
    try:
        d = json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        out["error"] = f"unreadable: {type(e).__name__}: {e}"[:200]
        return out
    if not isinstance(d, dict):
        out["error"] = "unexpected top-level type"
        return out
    try:
        out["rows"] = get_rows(d)
    except Exception:  # noqa: BLE001
        out["rows"] = None
    out["fileStatus"] = d.get("status")
    last = parse_date(get_date(d))
    fetched = parse_ts(d.get("fetchedAt"))
    out["lastDate"] = last.isoformat() if last else None
    out["fetchedAt"] = d.get("fetchedAt")

    today_ist = now.astimezone(IST).date()
    if last:
        out["ageBusinessDays"] = business_days_after(last, today_ist)
    if fetched:
        out["ageHours"] = round((now - fetched).total_seconds() / 3600, 1)
        out["ageBasis"] = "fetchedAt"
    elif last:
        start = datetime.combine(last, datetime.min.time(), IST)
        out["ageHours"] = round((now - start).total_seconds() / 3600, 1)
        out["ageBasis"] = "lastDate 00:00 IST"

    if kind == "bd":
        out["state"] = grade(out["ageBusinessDays"], spec) if last else "MISSING"
    else:
        if fetched:
            wh = round(weekday_hours_between(fetched, now), 1)
            out["ageWeekdayHours"] = wh
            out["state"] = grade(wh, spec)
        else:
            out["state"] = "MISSING"
    if out["state"] == "MISSING" and "error" not in out:
        out["error"] = "no date field found"
    return out


def run_info(env):
    run_id = env.get("GITHUB_RUN_ID")
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repo = env.get("GITHUB_REPOSITORY")
    steps = {}
    for s in STEPS:
        v = env.get("STEP_" + s.upper())
        if v:
            steps[s] = v
    if not steps:
        conclusion = "unknown" if run_id else "local"
    elif any(steps.get(s) not in (None, "success") for s in REQUIRED_STEPS):
        conclusion = "failure"
    elif any(v == "failure" for v in steps.values()):
        conclusion = "partial"  # an optional step failed; data still committed
    elif any(v == "cancelled" for v in steps.values()):
        conclusion = "cancelled"
    else:
        conclusion = "success"
    return {
        "runId": run_id,
        "runAttempt": env.get("GITHUB_RUN_ATTEMPT"),
        "runUrl": f"{server}/{repo}/actions/runs/{run_id}" if run_id and repo else None,
        "workflow": env.get("GITHUB_WORKFLOW"),
        "event": env.get("GITHUB_EVENT_NAME"),
        "sha": env.get("GITHUB_SHA"),
        "conclusion": conclusion,
        "conclusionNote": ("computed from step outcomes before the commit step; "
                           "'partial' = an optional (continue-on-error) step failed"),
        "steps": steps,
    }


def build(data_dir: Path, now: datetime, env) -> dict:
    feeds = {}
    for key in FEEDS:
        try:
            feeds[key] = feed_status(key, data_dir, now)
        except Exception as e:  # noqa: BLE001
            feeds[key] = {"key": key, "state": "MISSING",
                          "error": f"status error: {type(e).__name__}: {e}"[:200]}
    worst = max((f["state"] for f in feeds.values()), key=lambda s: STATE_RANK.get(s, 3))
    counts = {s: sum(1 for f in feeds.values() if f["state"] == s) for s in STATE_RANK}
    return {
        "schema": 1,
        "generatedAt": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "todayIST": now.astimezone(IST).date().isoformat(),
        "overall": worst,
        "counts": counts,
        "run": run_info(env),
        "feeds": feeds,
        "notes": [
            "Business days are Mon-Fri in IST; NSE holidays are not modelled, "
            "so a feed can read LATE the day after a market holiday.",
            "buffett is pushed by a Mac job (Mon-Fri); its LATE/STALE use weekday hours.",
        ],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--now", default=None, help="ISO timestamp, for testing")
    a = ap.parse_args(argv)
    data_dir = Path(a.data_dir)
    out = Path(a.out) if a.out else data_dir / "status.json"
    try:
        now = parse_ts(a.now) if a.now else datetime.now(timezone.utc)
        doc = build(data_dir, now, os.environ)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        doc = {"schema": 1, "overall": "MISSING",
               "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "error": f"{type(e).__name__}: {e}"[:300], "feeds": {}}
    try:
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=1) + "\n")
        tmp.replace(out)
        print(f"status.json: overall={doc.get('overall')} counts={doc.get('counts')}")
        for k, f in (doc.get("feeds") or {}).items():
            print(f"  {k:18s} {f.get('state'):8s} last={f.get('lastDate')} "
                  f"ageH={f.get('ageHours')} ageBD={f.get('ageBusinessDays')} rows={f.get('rows')}")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as e:
        sys.exit(0 if e.code in (0, None) else 0)
    except BaseException:  # noqa: BLE001 - never fail the Action
        traceback.print_exc()
        sys.exit(0)
