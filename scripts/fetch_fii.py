#!/usr/bin/env python3
"""Fetch FPI (FII) daily net equity flows from NSDL -> data/fii.json.   RUN ON A MAC, NOT IN THE ACTION.

NSDL's FPI Monitor (fpi.nsdl.co.in) resets connections from default user agents and,
like NSE/BSE, is not reliable from GitHub runner IPs. Jerry's Mac job
(system/bin/indexscope_fii_push.py) runs this from a residential IP and pushes fii.json only.

Source: https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx (ASP.NET form; one POST returns
one calendar month of daily rows). Depository-confirmed, equity only.
Definition notes (the page prints these):
  * A row's "Reporting Date" D carries the trades of the PREVIOUS trading day. The file keeps
    the reporting date and does not shift it.
  * `net`   = Equity, Stock Exchange route, net investment, Rs crore (like-for-like with NSE cash flows).
  * `total` = Equity sub-total (adds primary-market and other routes; what media usually quote).
  * `catchup` = 1 when gross turnover is > 1.8x the trailing 60-row median. It marks rows that LIKELY aggregate several trade days
    (it also flags some heavy single days). It is a caution flag, not proof.
History kept: from 2014-06-02. Incremental (>= 2000 existing rows): re-reads every month from the month of
the newest existing row (or the previous month, whichever is earlier) through this month and merges by date,
so a gap of any length is filled and no month is skipped.
Also records NSE's provisional same-day FII figure (cash market) as `nseToday`, labelled provisional.

Exit codes: 0 ok, 1 fetch/parse failure (file NOT overwritten). In incremental mode a run that parses
0 rows, or whose newest parsed row is more than 7 days old, is a failure too (NSDL returned old months only).
`--data PATH` reads/writes another file (testing against a copy); default data/fii.json.
"""
from __future__ import annotations

import argparse
import calendar
import html
import json
import re
import statistics
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

DATA = Path(__file__).resolve().parent.parent / "data" / "fii.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
URL = "https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx"
NSE_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
START = (2014, 6)
INCREMENTAL_MIN_ROWS = 2000
MAX_NEWEST_AGE_DAYS = 7   # incremental: newest parsed row older than this = NSDL served old months only
N = r"([\d.,()]+)"
ROW = re.compile(
    r"(\d{2}-[A-Za-z]{3}-\d{4}) Equity Stock Exchange " + N + " " + N + " " + N + " " + N
    + r"(?: Rs\.[\d.]+)? Primary market & others " + N + " " + N + " " + N + " " + N
    + " Sub-total " + N + " " + N + " " + N)


def num(s: str) -> float:
    s = s.strip().replace(",", "")
    neg = s.startswith("(")
    v = float(s.strip("()"))
    return -v if neg else v


def month_rows(sess: requests.Session, to_date: date) -> dict[str, tuple]:
    r = sess.get(URL, timeout=30)
    r.raise_for_status()
    f = {m.group(1): html.unescape(m.group(2)) for m in re.finditer(
        r'<input[^>]*name="(__VIEWSTATE|__VIEWSTATEGENERATOR|__EVENTVALIDATION)"[^>]*value="([^"]*)"', r.text)}
    if "__VIEWSTATE" not in f:
        raise RuntimeError("NSDL form fields missing; page layout changed")
    s = to_date.strftime("%d-%b-%Y")
    f.update({"__EVENTTARGET": "btnSubmit1", "__EVENTARGUMENT": "", "txtDate": s, "hdnDate": s,
              "hdnFlag": "", "HdnValexceldata": ""})
    r2 = sess.post(URL, data=f, timeout=60, headers={"Referer": URL})
    r2.raise_for_status()
    t = r2.text
    i = t.find("dvArchiveData")
    t = re.sub(r"<script.*?</script>", "", t[i:] if i >= 0 else t, flags=re.S)
    txt = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)))
    out = {}
    for m in ROW.finditer(txt):
        g = m.groups()
        d = datetime.strptime(g[0], "%d-%b-%Y").strftime("%Y-%m-%d")
        out[d] = (num(g[3]), num(g[11]), num(g[1]) + num(g[2]))   # se_net, subtotal_net, se gross (buy+sell)
    return out


def nse_today() -> dict | None:
    try:
        r = requests.get(NSE_URL, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        rows = {x["category"]: x for x in r.json()}
        fii = rows.get("FII/FPI") or rows.get("FII")
        if not fii:
            return None
        return {"date": datetime.strptime(fii["date"], "%d-%b-%Y").strftime("%Y-%m-%d"),
                "net": round(float(fii["netValue"]), 2), "buy": round(float(fii["buyValue"]), 2),
                "sell": round(float(fii["sellValue"]), 2),
                "provisional": True, "source": "NSE fiidiiTradeReact (cash market, provisional)"}
    except Exception as e:  # noqa: BLE001
        print(f"WARN nse_today: {e}", file=sys.stderr)
        return None


def start_month(existing: dict, today: date) -> tuple[int, int]:
    """First month to fetch. Full rebuild from START; incremental from the month of the newest existing row
    or the previous month, whichever is earlier, so a gap of any length is refilled and no month is skipped."""
    if len(existing) < INCREMENTAL_MIN_ROWS:
        return START
    newest = date.fromisoformat(max(existing))
    prev = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return min((newest.year, newest.month), prev)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch NSDL FPI daily equity flows -> data/fii.json")
    ap.add_argument("--data", type=Path, default=DATA, help="file to read and write (default data/fii.json)")
    path = ap.parse_args(argv).data
    existing = {}
    if path.exists():
        try:
            for d, net, tot, gross, _c in json.loads(path.read_text())["rows"]:
                existing[d] = (net, tot, gross)
        except Exception as e:  # noqa: BLE001
            print(f"WARN unreadable existing fii.json, rebuilding: {e}", file=sys.stderr)
            existing = {}
    today = date.today()
    incremental = len(existing) >= INCREMENTAL_MIN_ROWS
    y, m = start_month(existing, today)
    if incremental and existing:
        gap = (today - date.fromisoformat(max(existing))).days
        print(f"incremental from {y}-{m:02d} (newest existing row {max(existing)}, {gap}d old)")
    sess = requests.Session()
    sess.headers["User-Agent"] = UA
    got, newest_parsed = 0, None
    while (y, m) <= (today.year, today.month):
        to = min(date(y, m, calendar.monthrange(y, m)[1]), today)
        for attempt in range(3):
            try:
                rows = month_rows(sess, to)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    print(f"FAIL {to}: {e}", file=sys.stderr)
                    return 1
                time.sleep(3)
        existing.update(rows)
        got += len(rows)
        if rows:
            newest_parsed = max(newest_parsed or "", max(rows))
        m += 1
        if m == 13:
            y, m = y + 1, 1
        time.sleep(0.3)
    if incremental:
        if got == 0:
            print("FAIL: incremental run parsed 0 rows (NSDL layout change or empty months); refusing to write", file=sys.stderr)
            return 1
        age = (today - date.fromisoformat(newest_parsed)).days
        if age > MAX_NEWEST_AGE_DAYS:
            print(f"FAIL: newest parsed row {newest_parsed} is {age}d old (> {MAX_NEWEST_AGE_DAYS}); NSDL served old months only; refusing to write", file=sys.stderr)
            return 1
    if len(existing) < 2500 or not any(d >= START_ISO(today) for d in existing):
        print(f"FAIL: only {len(existing)} rows or no recent rows; refusing to write", file=sys.stderr)
        return 1

    dates = sorted(existing)
    rows_out, grosses = [], []
    for d in dates:
        net, tot, gross = existing[d]
        med = statistics.median(grosses[-60:]) if len(grosses) >= 20 else None
        catch = 1 if (med and gross > 1.8 * med) else 0
        rows_out.append([d, round(net, 2), round(tot, 2), round(gross, 2), catch])
        grosses.append(gross)
    out = {
        "status": "ok",
        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lastReportDate": dates[-1],
        "unit": "INR crore",
        "source": "NSDL FPI Monitor, Archive report (depository-confirmed)",
        "definition": "Equity net investment by foreign portfolio investors. net = Stock Exchange route; total = sub-total incl. primary market and other routes. "
                      "A row's date is the NSDL reporting date and carries the previous trading day's trades.",
        "cols": ["reportDate", "net", "total", "gross", "catchup"],
        "rows": rows_out,
        "nseToday": nse_today(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {path.name}: {len(rows_out)} rows, last {dates[-1]}, parsed {got} this run, nseToday={'yes' if out['nseToday'] else 'no'}")
    return 0


def START_ISO(today: date) -> str:
    """A row dated within the last 14 days must exist or the fetch silently returned old months only."""
    return date.fromordinal(today.toordinal() - 14).isoformat()


if __name__ == "__main__":
    sys.exit(main())
