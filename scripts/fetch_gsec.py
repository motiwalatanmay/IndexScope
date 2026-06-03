#!/usr/bin/env python3
"""Refresh data/gsec.json — India 10Y G-Sec daily yield series.

Used by the EY-BY (earnings-yield minus bond-yield) panel on the dashboard.
Source: the SectorScope worker /gsec endpoint, which returns the full daily
series [["Date","Yield"], [date, yield], ...] back to 2000. The endpoint is
Referer-gated to sectorscope.in, so we send that Referer.

Non-fatal by design: the frontend embeds a static GSEC_HISTORY fallback, so a
fetch failure here just means the current yield is a little stale until next run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent.parent / "data" / "gsec.json"
URL = "https://sectorscope.prashant-06f.workers.dev/gsec"


def main() -> int:
    try:
        r = requests.get(
            URL,
            headers={"Referer": "https://sectorscope.in/", "Origin": "https://sectorscope.in"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"gsec fetch failed: {e}", file=sys.stderr)
        return 1
    if not isinstance(data, list) or len(data) < 100:
        print(f"gsec payload looks wrong (len={len(data) if hasattr(data,'__len__') else '?'})", file=sys.stderr)
        return 1
    rows = data[1:] if (data and data[0] and data[0][0] == "Date") else data
    doc = {
        "status": "ok",
        "source": "India 10Y G-Sec (sectorscope worker /gsec)",
        "lastDate": rows[-1][0],
        "lastYield": rows[-1][1],
        "data": data,
    }
    OUT.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    print(f"wrote {OUT}  rows={len(rows)}  last={rows[-1][0]} {rows[-1][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
