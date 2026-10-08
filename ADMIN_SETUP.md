# Admin tab backend: setup, scope, testing, rollback

Backend for the IndexScope admin tab (the UI in `index.html` comes later).
Two parts:

1. **Pipeline status feed**: `scripts/write_status.py` writes `data/status.json`
   on every run of `.github/workflows/update-indices.yml`.
2. **Access control in the `indexscope-live` worker**: Google-verified admin,
   and an allow/deny list that gates the signed-in features (alerts).

The admin is `motiwalatanmay0@gmail.com`. The worker checks the Google ID token's
signature itself, so there are no shared passwords.

---

## Owner steps (in order)

Nothing below has been run yet. Run it from the repo root on the Mac.

1. **Run the tests (offline, no network).**
   ```sh
   node --test worker/test/*.test.mjs              # expect: pass 45, fail 0
   /opt/homebrew/bin/python3 scripts/write_status.py --out /tmp/status.json
   ```
2. **KV storage: nothing to create.** The worker already has a KV binding,
   `ALERTS` (id `a7164ba65a9e417c9cdbc5352ff3554c`), and the account records go
   into it under the `member:` and `acl:` key prefixes. No `USERS` namespace and
   no `wrangler kv namespace create` are needed.
3. **Admin emails and policy are already in `worker/wrangler.toml` `[vars]`:**
   ```toml
   ADMIN_EMAILS = "motiwalatanmay0@gmail.com"   # comma-separated
   DEFAULT_POLICY = "allow"                      # "deny" = allow-list mode
   SIGNIN_MIN_INTERVAL_SEC = "600"
   ```
   To add a second admin, edit `ADMIN_EMAILS` and redeploy. These are plain vars,
   not secrets. A deploy overwrites any value set in the Cloudflare dashboard.
4. **Deploy the worker.** This is the one command that changes production:
   ```sh
   cd worker && wrangler deploy
   ```
   The existing secrets (`JWT_SECRET`, `ADMIN_KEY`) are kept. Nothing new to set.
5. **Commit and push the Action change and the status script.** The worker does
   not need this step; it only affects `data/status.json`.
   ```sh
   git add .github/workflows/update-indices.yml scripts/write_status.py worker/test/jerry_pass.test.mjs \
           worker/src/worker.js worker/wrangler.toml worker/test/admin.test.mjs ADMIN_SETUP.md
   git commit -m "admin backend: status feed + worker allow/deny"
   git push
   ```
   Note: as of 2026-10-08 the workflow file also carries uncommitted macro/FII
   steps from separate work. Those steps call `scripts/fetch_macro.py` and
   `scripts/fetch_fii.py`, which are still untracked, so commit them together
   or the macro and FII steps will fail. They are continue-on-error, so a
   failure does not stop the run.
   Then start one run by hand: GitHub, then Actions, then "Update index data",
   then **Run workflow**. Check that `https://indexscope.in/data/status.json`
   appears.

---

## What is and is not protected

| Thing | Protected? | How |
|---|---|---|
| `POST /session` (alerts sign-in) | yes | 403 for a denied email |
| `GET/POST/DELETE /alerts`, `GET /alerts/all` | yes | 403 for a denied email on **every** request, so an existing 30-day session stops working once the deny propagates |
| Alert emails | yes | `/admin/export` leaves out denied users' alerts (`skippedDeniedUsers` in the response), so `eval_alerts.py` stops emailing them |
| `GET/PUT /admin/users` | admin only | `Authorization: Bearer <Google ID token>`; email must be in `ADMIN_EMAILS` |
| Dashboard, `data/*.json`, `data/status.json` | **no, by design** | public static files on GitHub Pages |
| Live-price proxy `GET /`, `/live` | **no, by design** | public, CORS `*` as before |
| The Google sign-in button itself | **no** | Google lets any account sign in. The UI must call `/signin` and hide signed-in features when `allowed` is `false` |

Policy, as implemented in `accessAllowed()` in `worker/src/worker.js`:
- an email listed in `ADMIN_EMAILS` is always allowed, and an admin cannot be denied;
- otherwise an explicit `acl:` status of `allow` or `deny` decides;
- otherwise `DEFAULT_POLICY` decides (`allow` by default).

Limits:
- **About 60 seconds of lag.** KV is eventually consistent, so a deny can take
  up to about a minute to reach every Cloudflare edge.
- **Admin tokens last about an hour.** Admin calls need a fresh Google ID token,
  which Google issues for about 1 hour. The UI should get a new one (Google
  One-Tap or the sign-in button) instead of reusing a stored one.
- **Sign-in counts are rate-limited.** `/signin` writes at most once per email
  per `SIGNIN_MIN_INTERVAL_SEC` (600 s). This protects the KV write quota, so
  `count` is the number of recorded sign-ins, not the number of attempts.
  Rate-limited calls still return `allowed` (with `recorded: false`).
- **`/session` takes Google ID tokens only.** It verifies the token locally
  against Google's keys, with the same checks as `/signin`: signature, issuer
  (`accounts.google.com`), audience (our client id), expiry and
  `email_verified`. The old OAuth *access token* path (Google userinfo, no
  audience check) is removed; `index.html` only ever sent the GIS `credential`.
- **Hardening (final critic, Group D):** a token with an unknown signing key
  refetches Google's keys at most once per 5 minutes per worker isolate. Every
  email (token claims, session tokens, `PUT /admin/users`) is capped at 254
  characters. `X-Admin-Key` is compared in constant time and fails closed when
  `ADMIN_KEY` is unset.
- **CORS:** every route except the live proxy now answers only
  `https://indexscope.in`, `https://www.indexscope.in` and
  `http://localhost:*` / `http://127.0.0.1:*`. Before this change the
  alerts/session routes allowed `*`. Server-to-server calls (`eval_alerts.py`)
  send no `Origin` header and are unaffected.

---

## Endpoint contract (for the UI)

All bodies are JSON. The token is the Google Identity Services `credential`, an ID token JWT.

- `POST /signin` `{token}` returns
  `{allowed, email, status: "allow"|"deny"|"none", isAdmin, recorded}`, or 401.
- `POST /admin/verify` `{token}` returns `{email, isAdmin}`, or 401 with `{reason}`.
- `POST /session` `{token}` returns `{sessionToken, expiresAt, email, name, picture}`,
  401 with `{reason}` for a bad token, or 403 for a denied email.
- `GET /admin/users` (header `Authorization: Bearer <token>`) returns
  `{defaultPolicy, count, users:[{email, status, allowed, isAdmin, firstSeen, lastSeen, count, name?, updatedAt?, updatedBy?}]}`.
- `PUT /admin/users` (same header) `{email, status: "allow"|"deny"}` returns `{ok, user}`.
  It returns 400 for a bad email, a bad status, or an attempt to deny an admin.
- Non-admin: 403. Missing or invalid token: 401.

The token checks: RS256 signature against Google's JWKS
(`https://www.googleapis.com/oauth2/v3/certs`, cached per its Cache-Control),
`iss` is `accounts.google.com`, `aud` is `GOOGLE_CLIENT_ID`, `exp` and `iat`
are within 60 s of skew, and `email_verified` is true.

`data/status.json` (written by `scripts/write_status.py`) contains:
`overall`, `counts`, `run{runId, runUrl, conclusion, steps{indices,global,...}}`,
and `feeds{key:{state, lastDate, fetchedAt, ageHours, ageBusinessDays, rows, thresholds, source}}`.
States are OK, LATE, STALE or MISSING. The Action feeds go LATE after more than
1 business day and STALE after more than 4. `buffett` is marked `source: mac-job`
and goes LATE after more than 12 and STALE after more than 36 weekday hours.
NSE holidays are not modelled.

---

## How to test after deploy

```sh
W=https://indexscope-live.motiwalatanmay0.workers.dev
# 1. Unauthenticated admin call should be 401
curl -s -o /dev/null -w '%{http_code}\n' $W/admin/users                 # 401
# 2. CORS: a foreign origin gets no Access-Control-Allow-Origin
curl -s -D - -o /dev/null -X OPTIONS -H 'Origin: https://evil.example' $W/admin/users | grep -i access-control-allow-origin || echo "no ACAO (good)"
# 3. With a real ID token (sign in on indexscope.in, copy the GIS credential):
TOKEN='<paste Google ID token>'
curl -s -X POST $W/admin/verify -H 'Content-Type: application/json' -d "{\"token\":\"$TOKEN\"}"
#   -> {"email":"motiwalatanmay0@gmail.com","isAdmin":true}
curl -s $W/admin/users -H "Authorization: Bearer $TOKEN"
curl -s -X PUT $W/admin/users -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"email":"someone@gmail.com","status":"deny"}'
# 4. The evaluator still works (uses the existing ADMIN_KEY secret):
curl -s $W/admin/export -H "X-Admin-Key: $ADMIN_KEY" | head -c 300
```

---

## Jerry pass (read-only key for Jerry on the Mac)

Jerry runs headless, so it cannot do a Google login. It uses a separate bearer key
(`JERRY_TOKEN`) that works on **two routes only**: `GET /admin/users` and
`GET /admin/health`.
- **Refused routes:** `PUT /admin/users` returns 403 and writes nothing.
  `/admin/verify`, `/signin`, `/session`, `/alerts*` and `/admin/export` all refuse the key.
- **Unset key:** if `JERRY_TOKEN` is unset or shorter than 24 characters, the pass is
  off and every pass request gets 401.
- **Wrong or missing key:** both get the same 401 body.
- **Comparison and logging:** the compare is constant-time (SHA-256 digests). The key
  is never logged or echoed, and `/session` never forwards it to Google.
- **Rate limit:** at most `PASS_RATE_PER_MIN` (20) pass attempts per IP per minute.
  This is best effort and counted separately by each Cloudflare isolate.
- **Google path unchanged:** the admin Google login keeps full access, including PUT.
- **Key format:** the worker treats any bearer value with exactly two dots as a Google
  ID token. A key from `openssl rand -base64 32` has no dots.

Client: `/opt/homebrew/bin/python3 system/bin/indexscope_admin.py health|users`
in Jerry. It reads the key only from the keychain (service `indexscope-jerry-pass`,
account `jerry`). Exit codes: 0 ok, 1 worker error or 429, 2 key missing or
rejected, 3 worker unreachable.

### Set up (owner, once)
```sh
cd ~/Desktop/IndexScope/worker
# 1. Make a key and copy it to the clipboard (it is never written to a file).
openssl rand -base64 32 | tr -d '\n' | pbcopy
# 2. Store it in the worker. Paste at the prompt.
wrangler secret put JERRY_TOKEN
# 3. Store the same key in the Mac keychain for Jerry. Paste at the prompt.
security add-generic-password -s indexscope-jerry-pass -a jerry -w
#    (-w with no value makes security prompt for it, so the key stays out of shell history)
# 4. Clear the clipboard.
pbcopy < /dev/null
# 5. Check (after the worker deploy that includes the pass code):
/opt/homebrew/bin/python3 ~/Documents/Jerry/system/bin/indexscope_admin.py health
```
`wrangler secret put` takes effect immediately on the deployed worker; no redeploy is needed.

### Rotate
```sh
openssl rand -base64 32 | tr -d '\n' | pbcopy
cd ~/Desktop/IndexScope/worker && wrangler secret put JERRY_TOKEN        # paste new key
security add-generic-password -U -s indexscope-jerry-pass -a jerry -w    # -U updates; paste new key
pbcopy < /dev/null
```
The old key stops working as soon as the secret is replaced. Jerry gets exit 2
until the keychain holds the new key.

### Revoke
```sh
cd ~/Desktop/IndexScope/worker && wrangler secret delete JERRY_TOKEN     # pass is off (fail closed: 401)
security delete-generic-password -s indexscope-jerry-pass -a jerry      # remove Jerry's copy
```
Deleting the secret is enough on its own to cut access. The Google admin path is
not affected.

## Roll back

- **Worker:** `cd worker && wrangler rollback`. This returns to the previous
  deployed version. To roll back from git instead: `git checkout <prev-sha> -- worker/src/worker.js worker/wrangler.toml`,
  then `wrangler deploy`. The `member:` and `acl:` KV keys are ignored by the old
  code, so you can leave them in place.
- **Un-deny one account in an emergency (no UI):**
  `cd worker && wrangler kv key delete --binding=ALERTS --remote "acl:someone@gmail.com"`
- **Leave allow-list mode:** set `DEFAULT_POLICY = "allow"` in `wrangler.toml`
  and run `wrangler deploy`. Explicit `deny` entries stay in force; delete
  their `acl:` keys as shown above to lift them.
- **Status feed:** revert `.github/workflows/update-indices.yml` (delete the two
  status steps and the `id:` lines) and delete `scripts/write_status.py` and
  `data/status.json`. The other steps never depend on them.
