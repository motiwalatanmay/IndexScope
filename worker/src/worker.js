// Cloudflare Worker for IndexScope.
//
// Two responsibilities:
//   1. Live-price proxy  — GET /  (and /live) proxies NSE /api/allIndices,
//      edge-cached 60s. Unchanged from the original cache-only worker.
//   2. Alerts backend    — Google-session auth + per-user valuation alerts
//      stored in Workers KV. Mirrors the SectorScope API contract:
//        POST   /session                  {token}            -> session JWT
//        POST   /alerts                    {index,metric,...} -> create (max 2/index)
//        GET    /alerts?index=KEY                             -> list for one index
//        GET    /alerts/all                                  -> all of the user's alerts
//        DELETE /alerts?index=KEY&id=ID                      -> remove one
//        GET    /admin/export   (X-Admin-Key)                -> all alerts (evaluator)
//
// Bindings (see wrangler.toml):
//   ALERTS            KV namespace   — alert + session storage
//   JWT_SECRET        secret         — HMAC key for session tokens
//   ADMIN_KEY         secret         — guards /admin/export for the GH-Action evaluator
//   GOOGLE_CLIENT_ID  var            — expected `aud` of Google ID tokens

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

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Admin-Key",
};

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });

    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    try {
      if (path === "/session" && request.method === "POST") return sessionHandler(request, env);
      if (path === "/alerts")                                return alertsHandler(request, env, url);
      if (path === "/alerts/all" && request.method === "GET") return alertsAllHandler(request, env);
      if (path === "/admin/export" && request.method === "GET") return adminExportHandler(request, env);
      if (path === "/" || path === "/live")                  return liveHandler(request);
    } catch (e) {
      return json({ status: "error", message: String(e && e.message || e) }, 500);
    }
    return json({ status: "error", message: "not found" }, 404);
  },
};

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

// Exchange a Google token (ID token JWT or OAuth2 access token) for our own
// HMAC-signed session token. Verifies the token directly with Google.
async function sessionHandler(request, env) {
  const { token } = await readJson(request);
  if (!token) return json({ error: "missing token" }, 400);

  let profile = null;
  if (token.split(".").length === 3) {
    // Looks like a Google ID token (JWT) — verify via tokeninfo.
    const r = await fetch("https://oauth2.googleapis.com/tokeninfo?id_token=" + encodeURIComponent(token));
    if (r.ok) {
      const p = await r.json();
      if (p.aud === env.GOOGLE_CLIENT_ID && p.email) {
        profile = { email: p.email, name: p.name || p.email, picture: p.picture || "" };
      }
    }
  }
  if (!profile) {
    // Fall back to treating it as an OAuth2 access token.
    const r = await fetch("https://www.googleapis.com/oauth2/v3/userinfo", {
      headers: { Authorization: "Bearer " + token },
    });
    if (r.ok) {
      const p = await r.json();
      if (p.email) profile = { email: p.email, name: p.name || p.email, picture: p.picture || "" };
    }
  }
  if (!profile) return json({ error: "invalid Google token" }, 401);

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
  if (!payload || !payload.email) return null;
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
  return json({ alerts: await getAlerts(env, user.email) });
}

// Evaluator-only: dump every alert across all users. Guarded by ADMIN_KEY.
async function adminExportHandler(request, env) {
  if (request.headers.get("X-Admin-Key") !== env.ADMIN_KEY) {
    return json({ error: "forbidden" }, 403);
  }
  const out = [];
  let cursor;
  do {
    const list = await env.ALERTS.list({ prefix: "user:", cursor });
    for (const k of list.keys) {
      const raw = await env.ALERTS.get(k.name);
      if (raw) { try { out.push(...JSON.parse(raw)); } catch (e) {} }
    }
    cursor = list.list_complete ? null : list.cursor;
  } while (cursor);
  return json({ alerts: out });
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
