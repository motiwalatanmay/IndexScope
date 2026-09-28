# CONTEXT — indexscope

Pointer doc per JERRY_ARCHITECTURE.md §9.2. Link, don't copy. Every claim cites a file; unverified claims are marked "inferred".

## Purpose
Static multi-index NSE valuation/timing dashboard at indexscope.in (GitHub Pages, custom domain via CNAME).
Covers 5 broad-market indices plus Buffett indicator, Global, and Projections tabs; a Cloudflare Worker overlays live intraday quotes.
Public-facing — a push to `main` can redeploy the live site (`[[project_indexscope]]`).

## Entry points
- GH Actions cron `.github/workflows/update-indices.yml` (03:30 UTC / 13:30 UTC = 09:00 / 19:00 IST) — runs `scripts/fetch_indices.py` + several `continue-on-error` enrichment steps (update-indices.yml:1-14, 20-30).
- Frontend: single inline `index.html` (all JS/CSS inline) — served by GitHub Pages.
- Cloudflare Worker `indexscope-live` in `worker/src/worker.js` — live-price proxy + alerts backend (worker.js:1-19).
- Workflow-file edits: `git push` is floored by a local safety hook; use `gh api --method PUT repos/motiwalatanmay/IndexScope/contents/<path>` instead (`[[project_indexscope]]`, "Workflow-file push gotcha").

## Inputs
- NSE `/api/allIndices`, fetched via `indexscope-live` worker first (edge egress, not datacenter-IP-blocked), direct-NSE as fallback (`scripts/fetch_indices.py`, `[[project_indexscope]]` "NSE fetch hardening").
- yfinance (13 global markets) — `scripts/fetch_global.py`.
- worldperatio.com / multpl.com — `scripts/fetch_global_val.py`.
- BSE `api.bseindia.com` total market cap — `scripts/fetch_buffett.py`; MOSPI/Budget GDP constants hand-edited in the same file on new prints.
- SectorScope Cloudflare Worker `https://sectorscope.prashant-06f.workers.dev/gsec` — India 10Y G-Sec series, Referer-gated to `sectorscope.in` (`scripts/fetch_gsec.py:21,28`). **External dependency owned by a different person (Prashant)** — not IndexScope's own worker.

## Outputs / served surfaces
- `data/{n50,nn50,nmid150,sc250,n500}.json`, `data/buffett.json`, `data/global.json`, `data/global_val.json`, `data/gsec.json`, `data/alert_state.json` — committed to the repo, read by `index.html` at runtime or injected via `scripts/_inject_data.py`.
- `https://indexscope-live.motiwalatanmay0.workers.dev/` (and `/live`) — live NSE quote proxy, 60s edge cache (worker.js:4-6, 34-35).
- Worker alerts API — `POST/GET/DELETE /alerts`, `/alerts/all`, `/session`, `/admin/export` (worker.js:8-13), KV namespace `ALERTS` (`worker/wrangler.toml:10-14`).

## Contracts
- `INDEX_MAP` (worker.js:21-27) must stay in sync with `INDICES`/`HISTORIES`/`INDEX_ORDER` in `index.html` and `INDICES` in `scripts/fetch_indices.py` — adding an index means updating all of these plus seeding `data/{key}.json` (`[[project_indexscope]]`, "Coverage").
- G-Sec fetch MUST send `Referer: https://sectorscope.in/` / `Origin: https://sectorscope.in` (`scripts/fetch_gsec.py:28`) — SectorScope's worker is Referer-gated; no header, no data.
- `worker/wrangler.toml:1-14` — `GOOGLE_CLIENT_ID` var + `ALERTS` KV binding are non-secret and CLI-deployed; `JWT_SECRET`/`ADMIN_KEY` are `wrangler secret put` only, never in the toml (wrangler.toml:16-18 comment).
- Buffett indicator denominator is always the LATEST REPORTED ACTUAL GDP, never a forecast — hand-maintained constants documented inline in `scripts/fetch_buffett.py` (`[[project_indexscope]]`, "Buffett Indicator tab").
- GH Action concurrency group `update-indices`, `cancel-in-progress: false` (update-indices.yml:11-13).

## Known failure modes
- **G-Sec dependency risk (design-flagged):** if Prashant's SectorScope worker changes or blocks, the daily G-Sec refresh breaks silently; the embedded static fallback series holds but the live yield goes stale (`[[project_indexscope]]`, "DEPENDENCY RISK"). No alerting exists on this — inferred gap.
- NSE periodically hard-blocks GitHub Actions' datacenter IPs; direct fetch timed out at 30s×3 before the fix. Mitigated by routing through the `indexscope-live` worker first, direct-NSE only as fallback (`[[project_indexscope]]`, "NSE fetch hardening", commit c183a91).
- Cookie-warmup-only retry was insufficient — the full session flow (warmup + API call) must be retried together, not just the payload call (same memory entry, "Lesson 2").
- Workflow-file (`.github/workflows/*.yml`) pushes get rejected by a stale osxkeychain-cached GitHub token lacking `workflow` scope even when `gh auth status` shows it — use `gh api ... contents PUT` instead (`[[project_indexscope]]`, "Workflow-file push gotcha").
- Buffett GDP denominator requires manual updates on MOSPI/Budget calendar events (Feb Budget BE, Jan FAE, May provisional) — a missed manual edit silently uses stale GDP, not a hard failure (`[[project_indexscope]]`, "Buffett Indicator tab" + "Data freshness & manual-update schedule").

## How to verify
- Data refresh ran: `gh run list --repo motiwalatanmay/IndexScope --workflow "Update index data" --limit 1 --json conclusion,status,createdAt` (pattern matches `research_ledger_health.py`'s `gh run list` use; no IndexScope-specific health script found — inferred command, not yet wired into `system/bin/`).
- Live worker reachable: `curl -s https://indexscope-live.motiwalatanmay0.workers.dev/` → JSON with `prices` for all 5 index keys (worker.js response shape, inferred from `INDEX_MAP`).
- Served-value-equals-source check: compare `data/n50.json`'s latest `pe` against `index.html`'s live-rendered Dashboard card for Nifty 50 during market hours — none found as an automated script; this is a manual/browser check today (inferred gap, relevant to T09).
- G-Sec Referer gate still open: `curl -s -H "Referer: https://sectorscope.in/" https://sectorscope.prashant-06f.workers.dev/gsec` → 200 with a JSON series; without the header, expect a block (`scripts/fetch_gsec.py:28` reproduces the required headers).

## Model-specific workarounds
None found — this unit is a static site + Python fetchers + a Cloudflare Worker; no `claude -p` or pinned-model call sits in its runtime path. "None found" per the design's own allowed answer (§9.2).

## Owners of live facts
- Index levels/PE/PB/DY: live `data/*.json` in this repo, refreshed by the GH Action — not any cached number in memory pages.
- G-Sec series: SectorScope's worker (external, Prashant-owned) — IndexScope only mirrors it into `data/gsec.json`.
- Buffett GDP constants: hand-maintained in `scripts/fetch_buffett.py`, per the MOSPI/Budget release calendar — not this file.
- Job/run health: `gh run list` against this repo's Actions, per the pattern in `~/Documents/Jerry/system/bin/research_ledger_health.py`.
