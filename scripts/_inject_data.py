#!/usr/bin/env python3
"""One-shot: embed N500_HISTORY + GSEC_HISTORY into index.html and seed data/n500.json.

Idempotent: removes any prior injected blocks first, then re-inserts. Run after
build_n500_history.py has produced data/n500_history.json.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "index.html"
N500 = ROOT / "data" / "n500_history.json"
GSEC = ROOT / "data" / "gsec.json"

BEGIN = "// === INJECTED CROSS-ASSET DATA (auto) ==="
END = "// === END INJECTED CROSS-ASSET DATA ==="


def main() -> int:
    n500 = json.loads(N500.read_text())
    gdoc = json.loads(GSEC.read_text())
    gseries = gdoc["data"]
    grows = gseries[1:] if (gseries and gseries[0] and gseries[0][0] == "Date") else gseries

    # Compact one-row-per-line not needed; single line each keeps file smaller.
    n500_js = "var N500_HISTORY = " + json.dumps(n500, separators=(",", ":")) + ";"
    gsec_js = "var GSEC_HISTORY = " + json.dumps([[d, y] for d, y in grows], separators=(",", ":")) + ";"
    block = f"{BEGIN}\n{n500_js}\n{gsec_js}\n{END}\n"

    html = HTML.read_text()
    # strip prior injection
    html = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n", "", html, flags=re.S)

    anchor = "var HISTORIES = {"
    assert anchor in html, "anchor not found"
    html = html.replace(anchor, block + anchor, 1)
    HTML.write_text(html)

    # seed data/n500.json with the most recent ~25 rows for the live-merge path
    recent = n500[-25:]
    doc = {
        "status": "ok",
        "fetchedAt": gdoc.get("lastDate", "") and "",  # placeholder; fetch_indices will set real one
        "data": recent,
        "lastDate": n500[-1][0],
        "rowCount": len(n500),
    }
    # fetchedAt must be a string; set to last date's ISO-ish
    doc["fetchedAt"] = n500[-1][0] + "T00:00:00Z"
    (ROOT / "data" / "n500.json").write_text(json.dumps(doc, separators=(",", ":")) + "\n")

    print(f"injected N500_HISTORY rows={len(n500)} ({n500[0][0]}..{n500[-1][0]})")
    print(f"injected GSEC_HISTORY rows={len(grows)} ({grows[0][0]}..{grows[-1][0]})")
    print(f"seeded data/n500.json with {len(recent)} recent rows")
    print(f"index.html size now {HTML.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
