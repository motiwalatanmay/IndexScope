#!/usr/bin/env python3
"""One-time seed: back-fill global trailing-P/E history from worldperatio.com
snapshots in the Wayback Machine.

Why this source: worldperatio is already the LIVE feed (fetch_global_val.py),
so its archived snapshots give a metric-consistent history — same basket, same
definition — with no splice artifact. Coverage is ~monthly from mid-2023.

Output: data/global_pe_history.json
  { "country": { "India": [["2023-07-24", 25.62], ...], ... },
    "byKey":   { "nifty": [...], "spx": [...], ... } }   # mapped to our keys

Run occasionally; the live daily scrape extends the series forward. Once ~3-5yr
has accrued, the Global valuation cards can switch from static bands to real
own-history percentile ranks (matching the India dashboard's methodology).
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# our index key -> worldperatio country (estoxx uses Germany/France mean)
KEY_TO_COUNTRY = {
    "nifty": "India", "spx": "United States", "csi300": "China",
    "hsi": "Hong Kong", "nikkei": "Japan", "kospi": "South Korea",
    "taiex": "Taiwan", "ftse": "United Kingdom", "estoxx": "__euro__",
}


def get(url: str, timeout: int = 45) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def cdx_snapshots() -> list[str]:
    """~monthly worldperatio snapshots (HTTP 200), oldest first."""
    url = ("http://web.archive.org/cdx/search/cdx?url=worldperatio.com"
           "&output=json&collapse=timestamp:6&filter=statuscode:200"
           "&from=2019&to=2026")
    rows = json.loads(get(url))
    ts_i = rows[0].index("timestamp")
    return [r[ts_i] for r in rows[1:]]


def parse_pes(html: str) -> dict:
    pairs = re.findall(
        r'"name":\s*"([^"]+)"(?:[^{}]*?)"desc":\s*\'[^\']*?([0-9]+\.[0-9]+)', html)
    return {k: float(v) for k, v in pairs}


def main() -> int:
    snaps = cdx_snapshots()
    print(f"{len(snaps)} snapshots {snaps[0][:8]}..{snaps[-1][:8]}")

    by_country: dict[str, list] = {}
    ok = 0
    for ts in snaps:
        date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
        try:
            # `id_` suffix returns the raw archived bytes (no Wayback toolbar)
            html = get(f"http://web.archive.org/web/{ts}id_/https://worldperatio.com/")
            pes = parse_pes(html)
            if not pes:
                print(f"  {date}: empty parse, skip", file=sys.stderr)
                continue
            for country, pe in pes.items():
                by_country.setdefault(country, []).append([date, round(pe, 2)])
            ok += 1
            print(f"  {date}: {len(pes)} countries (India={pes.get('India')})")
        except Exception as e:
            print(f"  {date}: FAIL {str(e)[:60]}", file=sys.stderr)
        time.sleep(1)  # be polite to archive.org

    if ok == 0:
        print("FATAL: no snapshots parsed", file=sys.stderr)
        return 1

    # de-dup by date (keep last) and sort each country series
    for c, series in by_country.items():
        seen = {}
        for d, v in series:
            seen[d] = v
        by_country[c] = sorted([[d, v] for d, v in seen.items()])

    def euro(date_series_lookup, dates):
        de = dict(by_country.get("Germany", []))
        fr = dict(by_country.get("France", []))
        out = []
        for d in sorted(set(de) & set(fr)):
            out.append([d, round((de[d] + fr[d]) / 2, 2)])
        return out

    by_key: dict[str, list] = {}
    for key, country in KEY_TO_COUNTRY.items():
        if country == "__euro__":
            by_key[key] = euro(by_country, None)
        elif country in by_country:
            by_key[key] = by_country[country]

    doc = {
        "source": "worldperatio.com via web.archive.org (Wayback)",
        "metric": "trailing P/E (country basket)",
        "note": "Metric-consistent seed for the live worldperatio feed. "
                "Series extend forward via fetch_global_val.py snapshots.",
        "snapshots": ok,
        "country": by_country,
        "byKey": by_key,
    }
    out = DATA_DIR / "global_pe_history.json"
    out.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    print(f"\nwrote {out}  ({ok} snapshots, {len(by_key)} mapped indices)")
    for key in KEY_TO_COUNTRY:
        s = by_key.get(key, [])
        if s:
            print(f"  {key:>7}: {len(s):2d} pts  {s[0][0]}={s[0][1]} .. {s[-1][0]}={s[-1][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
