#!/usr/bin/env python3
"""Evaluate user valuation alerts and email on threshold crossings.

Runs from GitHub Actions twice daily, right after fetch_indices.py. Read-only
against the site: it reuses the same "% of ref" valuation math the dashboard
shows (weighted winsorised 3/5/10yr medians), pulls users' alerts from the
Cloudflare Worker (/admin/export), and emails via Resend when an alert's
condition first becomes true. State is kept in data/alert_state.json so an
alert emails once per crossing, not every run.

Env:
  WORKER_BASE      e.g. https://indexscope-live.motiwalatanmay0.workers.dev
  ADMIN_KEY        matches the worker's ADMIN_KEY secret
  RESEND_API_KEY   Resend API key
  ALERT_FROM       verified sender, e.g. "IndexScope <alerts@indexscope.in>"
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
INDEX_HTML = ROOT / "index.html"
STATE_FILE = DATA_DIR / "alert_state.json"

# key -> embedded history var name in index.html
INDEX_HISTVAR = {
    "n50": "N50_HISTORY",
    "nn50": "NN50_HISTORY",
    "sc250": "SC250_HISTORY",
    "nmid150": "NMID150_HISTORY",
    "n500": "N500_HISTORY",
}
INDEX_NAME = {
    "n50": "Nifty 50",
    "nn50": "Nifty Next 50",
    "sc250": "Nifty Smallcap 250",
    "nmid150": "Nifty Midcap 150",
    "n500": "Nifty 500",
}
# Weighted reference weights — must match CFG.weights in index.html.
WEIGHTS = {"y3": 0.30, "y5": 0.60, "y10": 0.10}
# Column index in a history row [date, value, pe, pb, dy].
METRIC_COL = {"pe": 2, "pb": 3}


# ── valuation math (ports of the index.html helpers) ──────────────────

def _median(arr: list[float]):
    if not arr:
        return None
    s = sorted(arr)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def _winsorise(arr: list[float], lo_p=1, hi_p=99) -> list[float]:
    if not arr:
        return arr
    s = sorted(arr)
    n = len(s)
    lo = s[math.floor(lo_p / 100 * n)]
    hi = s[min(n - 1, math.ceil(hi_p / 100 * n))]
    return [max(lo, min(hi, v)) for v in arr]


def _winsorised_median(arr: list[float]):
    if not arr:
        return None
    return _median(_winsorise(arr, 1, 99))


def _weighted_ref(m3, m5, m10):
    total = 0.0
    val = 0.0
    for m, w in ((m3, WEIGHTS["y3"]), (m5, WEIGHTS["y5"]), (m10, WEIGHTS["y10"])):
        if m is not None:
            val += w * m
            total += w
    return (val / total) if total > 0 else None


def _date_add_years(date_str: str, y: int) -> str:
    d = date.fromisoformat(date_str)
    try:
        return d.replace(year=d.year + y).isoformat()
    except ValueError:  # Feb 29 in a non-leap target year -> roll to Mar 1, like JS
        return d.replace(year=d.year + y, month=3, day=1).isoformat()


def pct_of_ref(history: list[list], col: int):
    """Current metric value as % of the weighted winsorised reference."""
    rows = [r for r in history if len(r) > col and r[col] and r[col] > 0]
    if not rows:
        return None
    actual_date, actual = rows[-1][0], rows[-1][col]

    def vals(years: int) -> list[float]:
        frm = _date_add_years(actual_date, -years)
        return [r[col] for r in rows if frm <= r[0] <= actual_date]

    ref = _weighted_ref(_winsorised_median(vals(3)),
                        _winsorised_median(vals(5)),
                        _winsorised_median(vals(10)))
    return (actual / ref) * 100 if ref and ref > 0 else None


# ── data loading ──────────────────────────────────────────────────────

def load_embedded_history(var_name: str) -> list[list]:
    """Extract `var NAME = [ ... ];` from index.html and parse the JSON array."""
    text = INDEX_HTML.read_text()
    marker = f"var {var_name}"
    i = text.find(marker)
    if i < 0:
        return []
    start = text.find("[", i)
    end = text.find("];", start)
    if start < 0 or end < 0:
        return []
    return json.loads(text[start:end + 1])


def load_history(key: str) -> list[list]:
    """Full embedded history merged with the fresh tail in data/{key}.json."""
    hist = load_embedded_history(INDEX_HISTVAR[key])
    live = DATA_DIR / f"{key}.json"
    if live.exists():
        try:
            rows = json.loads(live.read_text()).get("data", [])
            by_date = {r[0]: r for r in hist}
            for r in rows:
                by_date[r[0]] = r
            hist = [by_date[d] for d in sorted(by_date)]
        except Exception as e:
            print(f"  warn: could not merge live {key}.json: {e}", file=sys.stderr)
    return hist


def current_values(key: str) -> dict:
    """{'pe_pct', 'pb_pct', 'level'} for an index, or Nones if unavailable."""
    hist = load_history(key)
    if not hist:
        return {"pe_pct": None, "pb_pct": None, "level": None}
    last = hist[-1]
    return {
        "pe_pct": pct_of_ref(hist, METRIC_COL["pe"]),
        "pb_pct": pct_of_ref(hist, METRIC_COL["pb"]),
        "pe_abs": last[2] if len(last) > 2 else None,
        "pb_abs": last[3] if len(last) > 3 else None,
        "level": last[1] if len(last) > 1 else None,
        "date": last[0],
    }


# ── alert evaluation ──────────────────────────────────────────────────

def observed_value(metric: str, cur: dict):
    if metric == "pe":
        return cur.get("pe_pct")
    if metric == "pb":
        return cur.get("pb_pct")
    if metric == "pe_abs":
        return cur.get("pe_abs")
    if metric == "pb_abs":
        return cur.get("pb_abs")
    if metric == "level":
        return cur.get("level")
    return None


def is_triggered(alert: dict, value) -> bool:
    if value is None:
        return False
    thr = float(alert["threshold"])
    return value <= thr if alert["direction"] == "below" else value >= thr


def describe(alert: dict) -> str:
    name = INDEX_NAME.get(alert["index"], alert["index"])
    cmp = "≥" if alert["direction"] == "above" else "≤"
    metric = alert["metric"]
    thr = alert["threshold"]
    if metric == "level":
        return f"{name} index level {cmp} {thr:g}"
    if metric == "pe_abs":
        return f"{name} P/E {cmp} {thr:g}"
    if metric == "pb_abs":
        return f"{name} P/B {cmp} {thr:g}"
    m = "P/E" if metric == "pe" else "P/B"
    return f"{name} {m} {cmp} {thr:g}% of reference"


def value_str(metric: str, value) -> str:
    if value is None:
        return "—"
    if metric == "level":
        return f"{value:,.2f}"
    if metric in ("pe_abs", "pb_abs"):
        return f"{value:.2f}"
    return f"{value:.1f}% of ref"


# ── email ─────────────────────────────────────────────────────────────

def send_email(to: str, subject: str, html: str) -> bool:
    api_key = os.environ["RESEND_API_KEY"]
    sender = os.environ.get("ALERT_FROM", "IndexScope <alerts@indexscope.in>")
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"from": sender, "to": [to], "subject": subject, "html": html},
        timeout=20,
    )
    if not r.ok:
        print(f"  email FAILED to {to}: {r.status_code} {r.text}", file=sys.stderr)
        return False
    return True


def alert_email_html(alert: dict, value, cur: dict) -> str:
    return f"""\
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px">
  <h2 style="margin:0 0 4px">🔔 IndexScope alert</h2>
  <p style="color:#555;margin:0 0 16px">Your valuation alert has triggered.</p>
  <table style="border-collapse:collapse;font-size:14px">
    <tr><td style="padding:4px 12px 4px 0;color:#777">Condition</td><td><strong>{describe(alert)}</strong></td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#777">Current</td><td>{value_str(alert['metric'], value)}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#777">As of</td><td>{cur.get('date','—')}</td></tr>
  </table>
  <p style="margin:18px 0 0"><a href="https://indexscope.in" style="color:#2563eb">Open IndexScope →</a></p>
  <p style="color:#999;font-size:11px;margin-top:24px">You set this alert on indexscope.in. Sign in and open Alerts to remove it.</p>
</div>"""


# ── main ──────────────────────────────────────────────────────────────

def fetch_alerts() -> list[dict]:
    base = os.environ["WORKER_BASE"].rstrip("/")
    r = requests.get(base + "/admin/export",
                     headers={"X-Admin-Key": os.environ["ADMIN_KEY"]}, timeout=20)
    r.raise_for_status()
    return r.json().get("alerts", [])


def main() -> int:
    try:
        alerts = fetch_alerts()
    except Exception as e:
        print(f"could not fetch alerts: {e}", file=sys.stderr)
        return 1
    print(f"loaded {len(alerts)} alert(s)")

    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            state = {}

    cache: dict[str, dict] = {}
    new_state: dict[str, bool] = {}
    sent = 0
    for a in alerts:
        key = a.get("index")
        if key not in INDEX_HISTVAR:
            continue
        if key not in cache:
            cache[key] = current_values(key)
        cur = cache[key]
        value = observed_value(a["metric"], cur)
        triggered = is_triggered(a, value)
        new_state[a["id"]] = triggered
        was = bool(state.get(a["id"], False))
        if triggered and not was:
            ok = send_email(
                a["email"],
                f"🔔 {describe(a)}",
                alert_email_html(a, value, cur),
            )
            if ok:
                sent += 1
                print(f"  emailed {a['email']}: {describe(a)} (now {value_str(a['metric'], value)})")

    STATE_FILE.write_text(json.dumps(new_state, indent=0, sort_keys=True) + "\n")
    print(f"done — {sent} email(s) sent, state has {len(new_state)} alert(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
