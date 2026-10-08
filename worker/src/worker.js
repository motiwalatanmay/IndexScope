// Cloudflare Worker for IndexScope.
//
// Two responsibilities:
//   1. Live-price proxy  — GET /  (and /live) proxies NSE /api/allIndices,
//      edge-cached 60s. Unchanged from the original cache-only worker.
//   2. Alerts backend    — Google-session auth + per-user valuation alerts
//      stored in Workers KV. Mirrors the SectorScope API contract:
//        POST   /session                  {token: Google ID token} -> session JWT
//        POST   /alerts                    {index,metric,...} -> create (max 2/index)
//        GET    /alerts?index=KEY                             -> list for one index
//        GET    /alerts/all                                  -> all of the user's alerts
//        DELETE /alerts?index=KEY&id=ID                      -> remove one
//        GET    /admin/export   (X-Admin-Key)                -> all alerts (evaluator;
//                                                               denied users excluded)
//   3. Sign-in access control (see ADMIN_SETUP.md):
//        POST   /signin         {token: Google ID token}     -> {allowed, email, status}
//        POST   /admin/verify   {token: Google ID token}     -> {email, isAdmin}
//        GET    /admin/users    (Bearer Google ID token, admin) -> every known account
//        PUT    /admin/users    {email, status: allow|deny}   -> set one account
//        GET    /admin/health   (admin or Jerry pass)         -> data/status.json + user counts
//      Denied accounts get 403 from /session and every /alerts route.
//   4. Jerry service pass (see ADMIN_SETUP.md): `Authorization: Bearer <JERRY_TOKEN>`
//      gives READ-ONLY admin access to GET /admin/users and GET /admin/health and
//      nothing else. Compared in constant time; disabled when JERRY_TOKEN is unset;
//      a wrong pass gets the same 401 as no token; per-IP per-minute limit.
//      Data files (data/*.json on GitHub Pages) stay public by design.
//
// Bindings (see wrangler.toml):
//   ALERTS            KV namespace   — alert + session storage, and account
//                                      records under the "member:" prefix
//   JWT_SECRET        secret         — HMAC key for session tokens
//   ADMIN_KEY         secret         — guards /admin/export for the GH-Action evaluator
//   GOOGLE_CLIENT_ID  var            — expected `aud` of Google ID tokens
//   ADMIN_EMAILS      var            — comma-separated admin Google accounts
//   DEFAULT_POLICY    var            — "allow" (deny-list mode) or "deny" (allow-list mode)
//   SIGNIN_MIN_INTERVAL_SEC var      — min seconds between /signin KV writes per email
//   JERRY_TOKEN       secret         — Jerry's read-only service pass (unset = pass off)
//   PASS_RATE_PER_MIN var            — per-IP limit on pass attempts and /admin/health
//   STATUS_URL        var            — where /admin/health reads status.json

const INDEX_MAP = {
  n50:     "NIFTY 50",
  nn50:    "NIFTY NEXT 50",
  nmid150: "NIFTY MIDCAP 150",
  sc250:   "NIFTY SMALLCAP 250",
  n500:    "NIFTY 500",
};
const VALID_INDEX = new Set(Object.keys(INDEX_MAP));
const VALID_METRIC = new Set(["pe", "pb", "pe_abs", "pb_abs", "level"]);
const VALID_DIR = new Set(["above", "below"]);
const MAX_PER_INDEX = 2;
const SESSION_DAYS = 30;

const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
const CACHE_TTL_SECONDS = 60;

// Public live-price proxy: open CORS (the data is public, like data/*.json).
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Admin-Key",
};

// Every signed-in / admin route answers only these origins (plus localhost for dev).
const ALLOWED_ORIGINS = new Set(["https://indexscope.in", "https://www.indexscope.in"]);
const LOCALHOST_ORIGIN = /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;
function originAllowed(origin) {
  return !!origin && (ALLOWED_ORIGINS.has(origin) || LOCALHOST_ORIGIN.test(origin));
}
function corsFor(request) {
  const origin = request.headers.get("Origin");
  const h = {
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Admin-Key",
    "Access-Control-Max-Age": "600",
    "Vary": "Origin",
  };
  if (originAllowed(origin)) h["Access-Control-Allow-Origin"] = origin;
  return h;
}

const GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs";
const GOOGLE_ISSUERS = new Set(["accounts.google.com", "https://accounts.google.com"]);
const CLOCK_SKEW_SEC = 60;
const MEMBER_PREFIX = "member:";   // sign-in activity, written by /signin
const ACL_PREFIX = "acl:";         // allow/deny status, written only by admins
const DEFAULT_SIGNIN_INTERVAL_SEC = 600;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const isPublic = path === "/" || path === "/live";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: isPublic ? CORS : corsFor(request) });
    }

    let resp;
    try {
      if (path === "/session" && request.method === "POST") resp = await sessionHandler(request, env);
      else if (path === "/alerts")                                resp = await alertsHandler(request, env, url);
      else if (path === "/alerts/all" && request.method === "GET") resp = await alertsAllHandler(request, env);
      else if (path === "/admin/export" && request.method === "GET") resp = await adminExportHandler(request, env);
      else if (path === "/signin" && request.method === "POST")  resp = await signinHandler(request, env);
      else if (path === "/admin/verify" && request.method === "POST") resp = await adminVerifyHandler(request, env);
      else if (path === "/admin/users" && (request.method === "GET" || request.method === "PUT"))
        resp = await adminUsersHandler(request, env);
      else if (path === "/admin/health" && request.method === "GET") resp = await adminHealthHandler(request, env);
      else if (isPublic) return liveHandler(request);
      else resp = json({ status: "error", message: "not found" }, 404);
    } catch (e) {
      resp = json({ status: "error", message: String(e && e.message || e) }, 500);
    }
    return withCors(resp, request);
  },
};

// Swap the open CORS headers that json() adds for the origin-restricted set.
function withCors(resp, request) {
  const out = new Response(resp.body, resp);
  for (const k of Object.keys(CORS)) out.headers.delete(k);
  for (const [k, v] of Object.entries(corsFor(request))) out.headers.set(k, v);
  return out;
}

/* ───────────────────────── live price proxy ───────────────────────── */

async function liveHandler(request) {
  const cache = caches.default;
  const cacheKey = new Request("https://indexscope-cache/live", request);
  const cached = await cache.match(cacheKey);
  if (cached) {
    const fresh = new Response(cached.body, cached);
    Object.entries(CORS).forEach(([k, v]) => fresh.headers.set(k, v));
    fresh.headers.set("X-Cache", "HIT");
    return fresh;
  }
  let body;
  try { body = await fetchLive(); }
  catch (e) { return json({ status: "error", message: String(e) }, 502); }
  const resp = json(body, 200, { "Cache-Control": `public, max-age=${CACHE_TTL_SECONDS}`, "X-Cache": "MISS" });
  await cache.put(cacheKey, resp.clone());
  return resp;
}

async function fetchLive() {
  const headers = {
    "User-Agent": UA,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
  };
  const warmup = await fetch("https://www.nseindia.com/", { headers });
  const setCookie = warmup.headers.get("set-cookie") || "";
  const cookieHeader = setCookie
    .split(/,(?=[^;]+=)/g)
    .map(c => c.split(";")[0].trim())
    .filter(Boolean)
    .join("; ");
  const apiResp = await fetch("https://www.nseindia.com/api/allIndices", {
    headers: { ...headers, Cookie: cookieHeader },
  });
  if (!apiResp.ok) throw new Error(`NSE returned ${apiResp.status}`);
  const payload = await apiResp.json();
  const out = { status: "ok", fetchedAt: new Date().toISOString(), prices: {} };
  for (const [key, nseName] of Object.entries(INDEX_MAP)) {
    const row = payload.data?.find(r => r.index === nseName);
    if (row && row.last != null) {
      out.prices[key] = {
        last: Number(row.last),
        pe: row.pe != null ? Number(row.pe) : null,
        pb: row.pb != null ? Number(row.pb) : null,
        dy: row.dy != null ? Number(row.dy) : null,
        perChange: row.percentChange != null ? Number(row.percentChange) : null,
      };
    }
  }
  return out;
}

/* ───────────────────────── session / auth ───────────────────────── */

// Exchange a Google ID token (the GIS credential) for our own HMAC-signed
// session token.
async function sessionHandler(request, env) {
  const { token } = await readJson(request);
  if (!token) return json({ error: "missing token" }, 400);
  // Only Google ID tokens (the GIS credential) are accepted, verified locally
  // against Google's keys: signature, iss, aud == GOOGLE_CLIENT_ID, exp and
  // email_verified. OAuth access tokens are refused: they carry no audience
  // check, so any app's token would have worked. A non-JWT (e.g. the Jerry
  // pass) fails as "malformed token" without any network call.
  const v = await verifyGoogleIdToken(token, env);
  if (!v.ok) return json({ error: "invalid Google token", reason: v.reason }, 401);
  const c = v.claims;
  const profile = { email: c.email, name: String(c.name || c.email).slice(0, 80), picture: String(c.picture || "") };
  if (await isDenied(env, profile.email)) return deniedResponse(profile.email);

  const expiresAt = Date.now() + SESSION_DAYS * 86400000;
  const sessionToken = await signJWT({ ...profile, exp: Math.floor(expiresAt / 1000) }, env.JWT_SECRET);
  return json({ sessionToken, expiresAt, ...profile });
}

// Returns the verified profile {email,name,picture} for a Bearer session token,
// or null. Replies are the caller's responsibility.
async function authUser(request, env) {
  const h = request.headers.get("Authorization") || "";
  const m = h.match(/^Bearer\s+(.+)$/i);
  if (!m) return null;
  const payload = await verifyJWT(m[1], env.JWT_SECRET);
  if (!payload || !validEmail(payload.email)) return null;
  if (payload.exp && payload.exp * 1000 < Date.now()) return null;
  return { email: payload.email, name: payload.name || payload.email, picture: payload.picture || "" };
}

/* ───────────────────────── alerts CRUD ───────────────────────── */

function userKey(email) { return "user:" + email.toLowerCase(); }
async function getAlerts(env, email) {
  const raw = await env.ALERTS.get(userKey(email));
  return raw ? JSON.parse(raw) : [];
}
async function putAlerts(env, email, alerts) {
  await env.ALERTS.put(userKey(email), JSON.stringify(alerts));
}

async function alertsHandler(request, env, url) {
  const user = await authUser(request, env);
  if (!user) return json({ error: "unauthorized" }, 401);
  // Session tokens live 30 days, so the deny list is checked on every request.
  if (await isDenied(env, user.email)) return deniedResponse(user.email);

  if (request.method === "GET") {
    const index = url.searchParams.get("index");
    const all = await getAlerts(env, user.email);
    const alerts = index ? all.filter(a => a.index === index) : all;
    return json({ alerts });
  }

  if (request.method === "POST") {
    const b = await readJson(request);
    const index = b.index;
    const metric = b.metric;
    const direction = b.direction;
    const threshold = Number(b.threshold);
    if (!VALID_INDEX.has(index)) return json({ error: "bad index" }, 400);
    if (!VALID_METRIC.has(metric)) return json({ error: "bad metric" }, 400);
    if (!VALID_DIR.has(direction)) return json({ error: "bad direction" }, 400);
    if (!isFinite(threshold) || threshold <= 0) return json({ error: "bad threshold" }, 400);

    const all = await getAlerts(env, user.email);
    if (all.filter(a => a.index === index).length >= MAX_PER_INDEX) {
      return json({ error: "limit reached" }, 409);
    }
    const alert = {
      id: crypto.randomUUID(),
      index, metric, direction, threshold,
      email: user.email,
      createdAt: new Date().toISOString(),
    };
    all.push(alert);
    await putAlerts(env, user.email, all);
    return json({ ok: true, alert });
  }

  if (request.method === "DELETE") {
    const index = url.searchParams.get("index");
    const id = url.searchParams.get("id");
    let all = await getAlerts(env, user.email);
    all = all.filter(a => !(a.id === id && (!index || a.index === index)));
    await putAlerts(env, user.email, all);
    return json({ ok: true });
  }

  return json({ error: "method not allowed" }, 405);
}

async function alertsAllHandler(request, env) {
  const user = await authUser(request, env);
  if (!user) return json({ error: "unauthorized" }, 401);
  if (await isDenied(env, user.email)) return deniedResponse(user.email);
  return json({ alerts: await getAlerts(env, user.email) });
}

// Evaluator-only: dump every alert across all users. Guarded by ADMIN_KEY.
// Alerts owned by denied accounts are left out, so they stop getting emails.
async function adminExportHandler(request, env) {
  // Fail closed when ADMIN_KEY is unset; constant-time compare otherwise.
  if (!env.ADMIN_KEY || !(await timingSafeEqualStr(request.headers.get("X-Admin-Key") || "", env.ADMIN_KEY))) {
    return json({ error: "forbidden" }, 403);
  }
  const members = await listMembers(env);
  const status = new Map(members.map(m => [m.email, m.status]));
  const out = [];
  let skipped = 0;
  let cursor;
  do {
    const list = await env.ALERTS.list({ prefix: "user:", cursor });
    for (const k of list.keys) {
      const raw = await env.ALERTS.get(k.name);
      if (!raw) continue;
      const email = k.name.slice("user:".length);
      if (!accessAllowed(env, email, status.get(email))) { skipped++; continue; }
      try { out.push(...JSON.parse(raw)); } catch (e) {}
    }
    cursor = list.list_complete ? null : list.cursor;
  } while (cursor);
  return json({ alerts: out, skippedDeniedUsers: skipped });
}

/* ───────────────────────── sign-in access control ───────────────────────── */

// Two record kinds in the ALERTS KV, each also stored as KV metadata so
// /admin/users can list everyone without one read per key:
//   member:<email>  {email, firstSeen, lastSeen, count, name?}   (/signin only)
//   acl:<email>     {email, status: allow|deny, updatedAt, updatedBy} (admin only)
// Keeping them apart means a /signin write can never overwrite an admin's deny.
// KV is eventually consistent: a deny can take up to ~60 s to reach every edge.

function normEmail(e) { return String(e || "").trim().toLowerCase(); }
// RFC 5321 caps a usable address at 254 characters. Checked on every route
// that takes an email (token claims, session tokens, PUT /admin/users).
const MAX_EMAIL_LEN = 254;
const EMAIL_RE = /^[^\s@,]+@[^\s@,]+\.[^\s@,]+$/;
function validEmail(e) {
  return typeof e === "string" && e.length > 0 && e.length <= MAX_EMAIL_LEN && EMAIL_RE.test(e);
}
function memberKey(email) { return MEMBER_PREFIX + normEmail(email); }
function aclKey(email) { return ACL_PREFIX + normEmail(email); }
function adminEmails(env) {
  return new Set(String(env.ADMIN_EMAILS || "").split(",").map(normEmail).filter(Boolean));
}
function isAdminEmail(env, email) { return adminEmails(env).has(normEmail(email)); }
function defaultPolicy(env) {
  return String(env.DEFAULT_POLICY || "allow").trim().toLowerCase() === "deny" ? "deny" : "allow";
}

// Pure policy: admins always allowed; explicit status wins; else DEFAULT_POLICY.
function accessAllowed(env, email, status) {
  if (isAdminEmail(env, email)) return true;
  if (status === "deny") return false;
  if (status === "allow") return true;
  return defaultPolicy(env) === "allow";
}

async function getRec(env, key) {
  const raw = await env.ALERTS.get(key);
  if (!raw) return null;
  try { return JSON.parse(raw); } catch (e) { return null; }
}
async function putRec(env, key, rec) {
  const meta = JSON.stringify(rec).length <= 1000 ? rec : { email: rec.email };
  await env.ALERTS.put(key, JSON.stringify(rec), { metadata: meta });
}
const getMember = (env, email) => getRec(env, memberKey(email));
const getAcl = (env, email) => getRec(env, aclKey(email));
async function getStatus(env, email) {
  const a = await getAcl(env, email);
  return (a && a.status) || "none";
}
async function listPrefix(env, prefix) {
  const out = [];
  let cursor;
  do {
    const list = await env.ALERTS.list({ prefix, cursor });
    for (const k of list.keys) {
      let rec = k.metadata;
      if (!rec || !rec.email) rec = await getRec(env, k.name);
      if (rec && rec.email) out.push(rec);
    }
    cursor = list.list_complete ? null : list.cursor;
  } while (cursor);
  return out;
}
// Merged view: one row per email seen by /signin or given a status by an admin.
async function listMembers(env) {
  const byEmail = new Map();
  for (const m of await listPrefix(env, MEMBER_PREFIX)) byEmail.set(m.email, { ...m, status: "none" });
  for (const a of await listPrefix(env, ACL_PREFIX)) {
    const cur = byEmail.get(a.email) || { email: a.email, count: 0 };
    byEmail.set(a.email, { ...cur, status: a.status, updatedAt: a.updatedAt, updatedBy: a.updatedBy });
  }
  return [...byEmail.values()];
}

async function isDenied(env, email) {
  if (isAdminEmail(env, email)) return false;
  return !accessAllowed(env, email, await getStatus(env, email));
}
function deniedResponse(email) {
  return json({
    error: "access denied",
    message: `The Google account ${normEmail(email)} is not allowed to use IndexScope's signed-in ` +
             "features (alerts). The public dashboard still works. Contact the site owner if this is a mistake.",
  }, 403);
}

// ---- Google ID token verification (RS256 against Google's JWKS) ----

let JWKS_CACHE = { keys: null, exp: 0 };
// A token with an unknown kid may mean Google rotated keys, so refetch, but at
// most once per 5 minutes per isolate: otherwise every forged-kid request
// would cost a fetch to Google.
const JWKS_FORCE_MIN_MS = 5 * 60 * 1000;
let JWKS_LAST_FORCED = 0;
async function googleKeys(forceRefresh) {
  if (!forceRefresh && JWKS_CACHE.keys && Date.now() < JWKS_CACHE.exp) return JWKS_CACHE.keys;
  const r = await fetch(GOOGLE_JWKS_URL);
  if (!r.ok) throw new Error(`Google JWKS returned ${r.status}`);
  const body = await r.json();
  const m = (r.headers.get("Cache-Control") || "").match(/max-age=(\d+)/);
  const ttl = Math.min(m ? Number(m[1]) : 3600, 86400) * 1000;
  JWKS_CACHE = { keys: body.keys || [], exp: Date.now() + ttl };
  return JWKS_CACHE.keys;
}

// Returns {ok:true, claims} or {ok:false, reason}. Never throws on bad input.
async function verifyGoogleIdToken(token, env) {
  if (!token || typeof token !== "string") return { ok: false, reason: "missing token" };
  const parts = token.split(".");
  if (parts.length !== 3) return { ok: false, reason: "malformed token" };
  let header, claims;
  try {
    header = JSON.parse(new TextDecoder().decode(bytesFromB64url(parts[0])));
    claims = JSON.parse(new TextDecoder().decode(bytesFromB64url(parts[1])));
  } catch (e) { return { ok: false, reason: "malformed token" }; }
  if (header.alg !== "RS256" || !header.kid) return { ok: false, reason: "unsupported token algorithm" };

  let keys = await googleKeys(false);
  let jwk = keys.find(k => k.kid === header.kid);
  if (!jwk && Date.now() - JWKS_LAST_FORCED >= JWKS_FORCE_MIN_MS) {
    JWKS_LAST_FORCED = Date.now();  // set before the fetch so failures are throttled too
    keys = await googleKeys(true);
    jwk = keys.find(k => k.kid === header.kid);
  }
  if (!jwk) return { ok: false, reason: "unknown signing key" };

  let valid = false;
  try {
    const key = await crypto.subtle.importKey(
      "jwk", { kty: jwk.kty, n: jwk.n, e: jwk.e, alg: "RS256", ext: true },
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    valid = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, bytesFromB64url(parts[2]),
      new TextEncoder().encode(parts[0] + "." + parts[1]));
  } catch (e) { valid = false; }
  if (!valid) return { ok: false, reason: "bad signature" };

  const now = Math.floor(Date.now() / 1000);
  if (!GOOGLE_ISSUERS.has(claims.iss)) return { ok: false, reason: "wrong issuer" };
  const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!env.GOOGLE_CLIENT_ID || !aud.includes(env.GOOGLE_CLIENT_ID)) return { ok: false, reason: "wrong audience" };
  if (typeof claims.exp !== "number" || claims.exp + CLOCK_SKEW_SEC < now) return { ok: false, reason: "token expired" };
  if (typeof claims.iat === "number" && claims.iat - CLOCK_SKEW_SEC > now) return { ok: false, reason: "token issued in the future" };
  if (!claims.email) return { ok: false, reason: "token has no email" };
  if (typeof claims.email !== "string" || claims.email.length > MAX_EMAIL_LEN) return { ok: false, reason: "bad email" };
  if (claims.email_verified !== true && claims.email_verified !== "true") {
    return { ok: false, reason: "email not verified" };
  }
  return { ok: true, claims: { ...claims, email: normEmail(claims.email) } };
}

async function tokenFromRequest(request) {
  const h = request.headers.get("Authorization") || "";
  const m = h.match(/^Bearer\s+(.+)$/i);
  if (m) return m[1].trim();
  if (request.method === "POST") {
    const b = await readJson(request);
    return b.token || b.credential || null;
  }
  return null;
}

// ---- handlers ----

// Any Google account: verify, record the sign-in, and say whether it may use
// signed-in features. Writes are rate-limited per email (KV write quota).
async function signinHandler(request, env) {
  const v = await verifyGoogleIdToken(await tokenFromRequest(request), env);
  if (!v.ok) return json({ error: "invalid Google token", reason: v.reason }, 401);
  const email = v.claims.email;
  const nowIso = new Date().toISOString();
  const prev = await getMember(env, email);
  const status = await getStatus(env, email);
  const allowed = accessAllowed(env, email, status);

  const minGap = Number(env.SIGNIN_MIN_INTERVAL_SEC || DEFAULT_SIGNIN_INTERVAL_SEC) * 1000;
  const last = prev && prev.lastSeen ? Date.parse(prev.lastSeen) : 0;
  let recorded = false;
  if (!prev || !(Date.now() - last < minGap)) {
    const rec = {
      email,
      firstSeen: (prev && prev.firstSeen) || nowIso,
      lastSeen: nowIso,
      count: ((prev && prev.count) || 0) + 1,
    };
    if (v.claims.name) rec.name = String(v.claims.name).slice(0, 80);
    await putRec(env, memberKey(email), rec);
    recorded = true;
  }
  return json({ allowed, email, status, isAdmin: isAdminEmail(env, email), recorded });
}

async function adminVerifyHandler(request, env) {
  const v = await verifyGoogleIdToken(await tokenFromRequest(request), env);
  if (!v.ok) return json({ error: "invalid Google token", reason: v.reason }, 401);
  return json({ email: v.claims.email, isAdmin: isAdminEmail(env, v.claims.email) });
}

// ---- Jerry service pass ----
// A bearer that is not JWT-shaped (no two dots) is a pass attempt. It is never
// sent to Google verification, and every failure (pass disabled, wrong pass)
// returns exactly the same 401 body as a request with no token at all.
const PASS_MIN_LEN = 24;
const DEFAULT_PASS_RATE_PER_MIN = 20;
const DEFAULT_STATUS_URL = "https://indexscope.in/data/status.json";
const NO_TOKEN_MSG = "send Authorization: Bearer <Google ID token>";
function unauthorized() { return json({ error: "unauthorized", message: NO_TOKEN_MSG }, 401); }
function bearerOf(request) {
  const m = (request.headers.get("Authorization") || "").match(/^Bearer\s+(.+)$/i);
  return m ? m[1].trim() : null;
}
function looksLikeJwt(tok) { return tok.split(".").length === 3; }

// Constant-time equality: compare SHA-256 digests (fixed length, so the input
// length does not leak) with an XOR accumulator and no early exit.
async function timingSafeEqualStr(a, b) {
  const enc = new TextEncoder();
  const [da, db] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(String(a))),
    crypto.subtle.digest("SHA-256", enc.encode(String(b))),
  ]);
  const x = new Uint8Array(da), y = new Uint8Array(db);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}
async function isValidPass(env, tok) {
  const secret = env.JERRY_TOKEN;
  if (typeof secret !== "string" || secret.length < PASS_MIN_LEN) return false;  // fail closed
  return timingSafeEqualStr(tok, secret);
}

// Per-IP, per-minute fixed window, kept in isolate memory (best effort: each
// Cloudflare isolate counts separately). No KV writes, nothing logged.
const RATE = new Map();
function rateLimited(request, env) {
  const limit = Number(env.PASS_RATE_PER_MIN || DEFAULT_PASS_RATE_PER_MIN);
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const win = Math.floor(Date.now() / 60000);
  const k = ip + "|" + win;
  const n = (RATE.get(k) || 0) + 1;
  RATE.set(k, n);
  if (RATE.size > 5000) for (const key of RATE.keys()) if (!key.endsWith("|" + win)) RATE.delete(key);
  return n > limit;
}
function tooMany() {
  return json({ error: "rate limited", message: "too many requests; try again in a minute" }, 429, { "Retry-After": "60" });
}

// Admin = a fresh Google ID token (Authorization: Bearer) whose email is in ADMIN_EMAILS,
// or, when opts.allowPass is set, the Jerry pass (returns {pass: true}, read-only).
async function requireAdmin(request, env, opts = {}) {
  const tok = bearerOf(request);
  if (!tok) return { resp: unauthorized() };
  if (!looksLikeJwt(tok)) {
    if (rateLimited(request, env)) return { resp: tooMany() };
    if (!(await isValidPass(env, tok))) return { resp: unauthorized() };
    if (!opts.allowPass) return { resp: json({ error: "forbidden", message: "the Jerry pass is read-only" }, 403) };
    return { email: "jerry-pass", pass: true };
  }
  const v = await verifyGoogleIdToken(tok, env);
  if (!v.ok) return { resp: json({ error: "invalid Google token", reason: v.reason }, 401) };
  if (!isAdminEmail(env, v.claims.email)) return { resp: json({ error: "forbidden", message: "admin only" }, 403) };
  return { email: v.claims.email };
}

async function adminUsersHandler(request, env) {
  // The Jerry pass may list users (GET) but never change them (PUT).
  const auth = await requireAdmin(request, env, { allowPass: request.method === "GET" });
  if (auth.resp) return auth.resp;

  if (request.method === "GET") {
    const members = await listMembers(env);
    const admins = adminEmails(env);
    for (const a of admins) {
      if (!members.find(m => m.email === a)) members.push({ email: a, status: "none", count: 0 });
    }
    const users = members.map(m => ({
      ...m,
      isAdmin: admins.has(m.email),
      allowed: accessAllowed(env, m.email, m.status),
    })).sort((a, b) => String(b.lastSeen || "").localeCompare(String(a.lastSeen || "")));
    return json({ defaultPolicy: defaultPolicy(env), count: users.length, users });
  }

  // PUT {email, status}
  const b = await readJson(request);
  if (typeof b.email !== "string" || b.email.length > MAX_EMAIL_LEN) return json({ error: "bad email" }, 400);
  const email = normEmail(b.email);
  const status = String(b.status || "").toLowerCase();
  if (!validEmail(email)) return json({ error: "bad email" }, 400);
  if (status !== "allow" && status !== "deny") return json({ error: "status must be allow or deny" }, 400);
  if (status === "deny" && isAdminEmail(env, email)) {
    return json({ error: "cannot deny an admin account; remove it from ADMIN_EMAILS first" }, 400);
  }
  const acl = { email, status, updatedAt: new Date().toISOString(), updatedBy: auth.email };
  await putRec(env, aclKey(email), acl);
  const seen = (await getMember(env, email)) || { email, count: 0 };
  return json({ ok: true, user: { ...seen, ...acl, isAdmin: isAdminEmail(env, email), allowed: accessAllowed(env, email, status) } });
}

// GET /admin/health: the published data/status.json plus account counts and the
// most recent /signin times. Readable by an admin or the Jerry pass.
async function adminHealthHandler(request, env) {
  const tok = bearerOf(request);
  if (tok && looksLikeJwt(tok) && rateLimited(request, env)) return tooMany();
  const auth = await requireAdmin(request, env, { allowPass: true });
  if (auth.resp) return auth.resp;

  let status = null, statusError = null;
  try {
    const r = await fetch(env.STATUS_URL || DEFAULT_STATUS_URL, { cf: { cacheTtl: 60 } });
    if (r.ok) status = await r.json();
    else statusError = `status.json returned HTTP ${r.status}`;
  } catch (e) { statusError = "status.json fetch failed: " + String(e && e.message || e); }

  const members = await listMembers(env);
  let allowed = 0, denied = 0;
  for (const m of members) (accessAllowed(env, m.email, m.status) ? allowed++ : denied++);
  const signins = members.filter(m => m.lastSeen)
    .sort((a, b) => String(b.lastSeen).localeCompare(String(a.lastSeen)));
  return json({
    generatedAt: new Date().toISOString(),
    via: auth.pass ? "jerry-pass" : "admin",
    status, statusError,
    users: {
      defaultPolicy: defaultPolicy(env), total: members.length, allowed, denied,
      explicitAllow: members.filter(m => m.status === "allow").length,
      explicitDeny: members.filter(m => m.status === "deny").length,
    },
    lastSigninAt: signins.length ? signins[0].lastSeen : null,
    recentSignins: signins.slice(0, 10).map(m => ({
      email: m.email, lastSeen: m.lastSeen, count: m.count || 0,
      allowed: accessAllowed(env, m.email, m.status),
    })),
  });
}

/* ───────────────────────── HMAC-SHA256 JWT ───────────────────────── */

function b64urlFromBytes(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function b64urlFromStr(str) { return b64urlFromBytes(new TextEncoder().encode(str)); }
function bytesFromB64url(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
async function hmacKey(secret) {
  return crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"],
  );
}
async function signJWT(payload, secret) {
  const head = b64urlFromStr(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const body = b64urlFromStr(JSON.stringify(payload));
  const data = head + "." + body;
  const key = await hmacKey(secret);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(data));
  return data + "." + b64urlFromBytes(new Uint8Array(sig));
}
async function verifyJWT(tok, secret) {
  const parts = tok.split(".");
  if (parts.length !== 3) return null;
  const data = parts[0] + "." + parts[1];
  const key = await hmacKey(secret);
  const ok = await crypto.subtle.verify("HMAC", key, bytesFromB64url(parts[2]), new TextEncoder().encode(data));
  if (!ok) return null;
  try { return JSON.parse(new TextDecoder().decode(bytesFromB64url(parts[1]))); }
  catch (e) { return null; }
}

/* ───────────────────────── helpers ───────────────────────── */

async function readJson(request) {
  try { return await request.json(); } catch (e) { return {}; }
}
function json(obj, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...CORS, ...extraHeaders },
  });
}
