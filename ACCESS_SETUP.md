# Hard gate via Cloudflare Access (Zero Trust)

Goal: only approved people can load **anything** on indexscope.in — including the
`data/*.json` files — enforced at Cloudflare's edge, no app code. Free for up to
**50 users**. This replaces the soft Firebase gate (leave `FIREBASE_CONFIG` blank
so that one stays off; Access does the auth now).

> **Why this is safe for you:** a DNS snapshot on 2026-06-03 showed your domain has
> **no email (MX), no SPF/DKIM/TXT, and no extra subdomains** — only the GitHub
> Pages records below. So the usual migration risk (breaking email) does not apply.

## Your current DNS (the ONLY records to recreate)

| Type | Name | Value |
|---|---|---|
| A | `@` (indexscope.in) | `185.199.108.153` |
| A | `@` | `185.199.109.153` |
| A | `@` | `185.199.110.153` |
| A | `@` | `185.199.111.153` |
| CNAME | `www` | `indexscope.in` |

That's everything. Cloudflare auto-scans and imports these when you add the site —
just verify all five are present before you switch nameservers.

---

## Part A — Move DNS to Cloudflare (~15 min + propagation)

1. **Create a free Cloudflare account** (or use your existing one — you already run
   workers there) → **Add a site** → enter `indexscope.in` → choose the **Free** plan.
2. Cloudflare scans existing DNS. **Verify** the 5 records above are present. If `www`
   didn't import, add it: CNAME `www` → `indexscope.in`.
3. Set all five records to **Proxied (orange cloud)** — this is required for Access to
   work. (Grey-cloud / DNS-only will NOT gate the site.)
4. **SSL/TLS → Overview → set mode to `Full`.** ⚠️ Do **not** use `Flexible` — it
   causes a redirect loop with GitHub Pages (Pages forces HTTPS).
5. **SSL/TLS → Edge Certificates → turn ON "Always Use HTTPS".**
6. Cloudflare shows you **two nameservers** (e.g. `xxx.ns.cloudflare.com`). Copy them.
7. **At GoDaddy:** Domain → **Nameservers → Change → "I'll use my own nameservers"** →
   replace the two `domaincontrol.com` entries with the two Cloudflare ones → Save.
8. Wait for activation (usually 30 min–2 h; Cloudflare emails you). Confirm with:
   `curl -sI https://indexscope.in | grep -i cf-ray` — a `cf-ray` header means traffic
   now flows through Cloudflare.
9. Sanity-check the site still loads normally over HTTPS before adding the gate.

> GitHub side: leave Pages **custom domain = indexscope.in** and **Enforce HTTPS = on**.
> GitHub already provisioned a valid cert, so Cloudflare `Full` mode validates fine.

---

## Part B — Turn on the access gate (~10 min)

1. In the Cloudflare dashboard, open **Zero Trust** (left sidebar; first time it asks
   you to pick a team name and the **Free** plan — choose Free).
2. **Access → Applications → Add an application → Self-hosted.**
3. **Application configuration:**
   - Name: `IndexScope`
   - Session duration: `24 hours` (or `1 week` for less re-login)
   - Add **two** public hostnames (Domain): `indexscope.in` **and** `www.indexscope.in`
     (or a single subdomain `*` wildcard if offered).
4. **Add a policy:**
   - Policy name: `Approved users`
   - Action: **Allow**
   - Configure rules → Include → **Emails** → paste the approved email addresses
     (your ≤30 users), comma/Enter separated.
     - *Alternative:* "Emails ending in" `@yourcompany.com` to allow a whole domain.
5. **Choose how they log in** (Authentication):
   - Easiest, zero setup: **One-time PIN** is on by default — users enter their email
     and get a 6-digit code. Perfect for ≤30 people.
   - Optional one-click: **Settings → Authentication → Add Google** as a login method.
6. Save. Open `https://indexscope.in` in an **incognito window** — you should hit a
   Cloudflare login page; only an allow-listed email gets in, everything else is blocked.

---

## Day-to-day

- **Add / remove a user:** Zero Trust → Access → Applications → IndexScope → edit the
  **Approved users** policy → change the email list. Instant.
- **See who's using it:** Zero Trust → **Logs → Access** — every login attempt with
  email, time, and allow/block result. (For richer traffic stats, enable **Cloudflare
  Web Analytics** on the site — free, no login needed.)
- **Turn the gate off temporarily:** delete (or disable) the Access application; the
  site reverts to fully public instantly.

## Notes

- This is a **hard gate**: unauthenticated requests never reach GitHub Pages, so the
  JSON data is not fetchable without logging in.
- The `indexscope-live` worker (live-price overlay) is on a separate `workers.dev`
  host and isn't gated by this — it only serves public NSE quote data, not the
  valuation history, so that's fine. (It can be protected separately later if wanted.)
- The Firebase sign-in code added earlier stays **inert** (blank `FIREBASE_CONFIG`).
  You can delete that block later for tidiness; it has no effect while unconfigured.
