// Offline tests for the sign-in access-control routes in worker/src/worker.js.
// No network, no wrangler: Google's JWKS and tokeninfo are mocked, KV is an
// in-memory fake. Run from the repo root:
//   node --test worker/test/*.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";

const worker = (await import(new URL("../src/worker.js", import.meta.url))).default;

const CLIENT_ID = "583500951310-6dapvdgbe6je6k6mi87qn2jt3ori90cj.apps.googleusercontent.com";
const ADMIN = "motiwalatanmay0@gmail.com";
const KID = "test-kid-1";

// ---------- fake KV ----------
class FakeKV {
  constructor() { this.m = new Map(); this.writes = 0; }
  async get(k) { return this.m.has(k) ? this.m.get(k).v : null; }
  async put(k, v, opts = {}) { this.writes++; this.m.set(k, { v, meta: opts.metadata ?? null }); }
  async delete(k) { this.m.delete(k); }
  async list({ prefix = "" } = {}) {
    const keys = [...this.m.keys()].filter(k => k.startsWith(prefix)).sort()
      .map(name => ({ name, metadata: this.m.get(name).meta }));
    return { keys, list_complete: true, cursor: null };
  }
}

function makeEnv(over = {}) {
  return {
    ALERTS: new FakeKV(),
    GOOGLE_CLIENT_ID: CLIENT_ID,
    ADMIN_EMAILS: ADMIN,
    DEFAULT_POLICY: "allow",
    SIGNIN_MIN_INTERVAL_SEC: "600",
    JWT_SECRET: "test-secret-not-real",
    ADMIN_KEY: "test-admin-key",
    ...over,
  };
}

// ---------- token factory ----------
const b64u = (buf) => Buffer.from(buf).toString("base64url");
const algo = { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" };
const good = await webcrypto.subtle.generateKey(algo, true, ["sign", "verify"]);
const evil = await webcrypto.subtle.generateKey(algo, true, ["sign", "verify"]);
const pubJwk = { ...(await webcrypto.subtle.exportKey("jwk", good.publicKey)), kid: KID, alg: "RS256", use: "sig" };

async function makeToken(claims = {}, { key = good.privateKey, kid = KID, alg = "RS256" } = {}) {
  const now = Math.floor(Date.now() / 1000);
  const payload = {
    iss: "https://accounts.google.com", aud: CLIENT_ID, sub: "1234",
    email: "user@gmail.com", email_verified: true, name: "Test User",
    iat: now - 10, exp: now + 3600, ...claims,
  };
  const head = b64u(JSON.stringify({ alg, kid, typ: "JWT" }));
  const body = b64u(JSON.stringify(payload));
  const sig = await webcrypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(head + "." + body));
  return head + "." + body + "." + b64u(sig);
}

// ---------- fetch mock: Google JWKS + tokeninfo ----------
let jwksFetches = 0;
globalThis.fetch = async (input) => {
  const url = typeof input === "string" ? input : input.url;
  if (url.startsWith("https://www.googleapis.com/oauth2/v3/certs")) {
    jwksFetches++;
    return new Response(JSON.stringify({ keys: [pubJwk] }), {
      headers: { "Content-Type": "application/json", "Cache-Control": "public, max-age=3600" },
    });
  }
  if (url.startsWith("https://oauth2.googleapis.com/tokeninfo?id_token=")) {
    // Mirrors Google: echo the claims of a token we minted (signature checked by the real service).
    const tok = decodeURIComponent(url.split("id_token=")[1]);
    const claims = JSON.parse(Buffer.from(tok.split(".")[1], "base64url").toString());
    return new Response(JSON.stringify(claims), { headers: { "Content-Type": "application/json" } });
  }
  throw new Error("unexpected network call in test: " + url);
};

// ---------- request helper ----------
async function call(env, method, path, { body, token, origin = "https://indexscope.in", headers = {} } = {}) {
  const h = { ...headers };
  if (origin) h.Origin = origin;
  if (token) h.Authorization = "Bearer " + token;
  if (body !== undefined) h["Content-Type"] = "application/json";
  const req = new Request("https://indexscope-live.example" + path, {
    method, headers: h, body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const res = await worker.fetch(req, env);
  const text = await res.text();
  let data = null; try { data = JSON.parse(text); } catch (e) { data = text; }
  return { status: res.status, data, headers: res.headers };
}

// ================= /admin/verify =================
test("verify: valid admin token -> isAdmin true", async () => {
  const env = makeEnv();
  const r = await call(env, "POST", "/admin/verify", { body: { token: await makeToken({ email: ADMIN }) } });
  assert.equal(r.status, 200);
  assert.deepEqual(r.data, { email: ADMIN, isAdmin: true });
});

test("verify: admin match is case-insensitive", async () => {
  const r = await call(makeEnv(), "POST", "/admin/verify", { body: { token: await makeToken({ email: "MotiwalaTanmay0@Gmail.com" }) } });
  assert.equal(r.status, 200);
  assert.equal(r.data.isAdmin, true);
});

test("verify: valid non-admin token -> isAdmin false", async () => {
  const r = await call(makeEnv(), "POST", "/admin/verify", { body: { token: await makeToken() } });
  assert.equal(r.status, 200);
  assert.deepEqual(r.data, { email: "user@gmail.com", isAdmin: false });
});

const badCases = [
  ["expired", () => makeToken({ email: ADMIN, exp: Math.floor(Date.now() / 1000) - 3600 }), "token expired"],
  ["wrong audience", () => makeToken({ email: ADMIN, aud: "someone-else.apps.googleusercontent.com" }), "wrong audience"],
  ["wrong signature", () => makeToken({ email: ADMIN }, { key: evil.privateKey }), "bad signature"],
  ["unknown kid", () => makeToken({ email: ADMIN }, { kid: "nope" }), "unknown signing key"],
  ["wrong issuer", () => makeToken({ email: ADMIN, iss: "https://evil.example" }), "wrong issuer"],
  ["unverified email", () => makeToken({ email: ADMIN, email_verified: false }), "email not verified"],
  ["alg none", async () => { const t = await makeToken({ email: ADMIN }); return t.replace(/^[^.]+/, b64u(JSON.stringify({ alg: "none", kid: KID }))); }, "unsupported token algorithm"],
  ["malformed", async () => "not.a-token", "malformed token"],
  ["tampered payload", async () => { const t = (await makeToken()).split("."); t[1] = b64u(JSON.stringify({ iss: "https://accounts.google.com", aud: CLIENT_ID, email: ADMIN, email_verified: true, exp: 9e9 })); return t.join("."); }, "bad signature"],
];
for (const [name, mk, reason] of badCases) {
  test(`verify: ${name} -> 401 (${reason})`, async () => {
    const r = await call(makeEnv(), "POST", "/admin/verify", { body: { token: await mk() } });
    assert.equal(r.status, 401);
    assert.equal(r.data.reason, reason);
  });
}

test("verify: missing token -> 401", async () => {
  const r = await call(makeEnv(), "POST", "/admin/verify", { body: {} });
  assert.equal(r.status, 401);
});

// ================= /admin/users =================
test("users: no token -> 401", async () => {
  const r = await call(makeEnv(), "GET", "/admin/users");
  assert.equal(r.status, 401);
});

test("users: non-admin GET and PUT -> 403", async () => {
  const env = makeEnv();
  const tok = await makeToken({ email: "user@gmail.com" });
  assert.equal((await call(env, "GET", "/admin/users", { token: tok })).status, 403);
  const p = await call(env, "PUT", "/admin/users", { token: tok, body: { email: "x@gmail.com", status: "deny" } });
  assert.equal(p.status, 403);
  assert.equal(env.ALERTS.writes, 0);
});

test("users: expired admin token -> 401", async () => {
  const tok = await makeToken({ email: ADMIN, exp: Math.floor(Date.now() / 1000) - 3600 });
  assert.equal((await call(makeEnv(), "GET", "/admin/users", { token: tok })).status, 401);
});

test("users: admin lists sign-ins, sets deny, validation errors", async () => {
  const env = makeEnv();
  await call(env, "POST", "/signin", { body: { token: await makeToken({ email: "a@gmail.com" }) } });
  const admin = await makeToken({ email: ADMIN });
  let r = await call(env, "GET", "/admin/users", { token: admin });
  assert.equal(r.status, 200);
  assert.equal(r.data.defaultPolicy, "allow");
  const a = r.data.users.find(u => u.email === "a@gmail.com");
  assert.equal(a.count, 1); assert.equal(a.status, "none"); assert.equal(a.allowed, true);
  assert.ok(r.data.users.find(u => u.email === ADMIN && u.isAdmin));

  r = await call(env, "PUT", "/admin/users", { token: admin, body: { email: "A@Gmail.com", status: "deny" } });
  assert.equal(r.status, 200);
  assert.equal(r.data.user.email, "a@gmail.com");
  assert.equal(r.data.user.allowed, false);
  assert.equal(r.data.user.updatedBy, ADMIN);

  r = await call(env, "GET", "/admin/users", { token: admin });
  const a2 = r.data.users.find(u => u.email === "a@gmail.com");
  assert.equal(a2.status, "deny"); assert.equal(a2.count, 1);

  assert.equal((await call(env, "PUT", "/admin/users", { token: admin, body: { email: "b@gmail.com", status: "maybe" } })).status, 400);
  assert.equal((await call(env, "PUT", "/admin/users", { token: admin, body: { email: "not-an-email", status: "deny" } })).status, 400);
  assert.equal((await call(env, "PUT", "/admin/users", { token: admin, body: { email: ADMIN, status: "deny" } })).status, 400);
});

// ================= /signin =================
test("signin: new user allowed and recorded; second call within interval not written", async () => {
  const env = makeEnv();
  const tok = await makeToken({ email: "new@gmail.com" });
  let r = await call(env, "POST", "/signin", { body: { token: tok } });
  assert.equal(r.status, 200);
  assert.equal(r.data.allowed, true); assert.equal(r.data.recorded, true);
  const w = env.ALERTS.writes;
  r = await call(env, "POST", "/signin", { body: { token: tok } });
  assert.equal(r.data.recorded, false);
  assert.equal(env.ALERTS.writes, w, "rate-limited: no KV write");
  const rec = JSON.parse(await env.ALERTS.get("member:new@gmail.com"));
  assert.equal(rec.count, 1); assert.ok(rec.firstSeen); assert.ok(rec.lastSeen);
});

test("signin: after the interval, count increments and firstSeen is kept", async () => {
  const env = makeEnv({ SIGNIN_MIN_INTERVAL_SEC: "0" });
  const tok = await makeToken({ email: "r@gmail.com" });
  await call(env, "POST", "/signin", { body: { token: tok } });
  const first = JSON.parse(await env.ALERTS.get("member:r@gmail.com"));
  await new Promise(r => setTimeout(r, 5));
  await call(env, "POST", "/signin", { body: { token: tok } });
  const second = JSON.parse(await env.ALERTS.get("member:r@gmail.com"));
  assert.equal(second.count, 2); assert.equal(second.firstSeen, first.firstSeen);
});

test("signin: denied user -> allowed false, and a later sign-in cannot clear the deny", async () => {
  const env = makeEnv({ SIGNIN_MIN_INTERVAL_SEC: "0" });
  const admin = await makeToken({ email: ADMIN });
  await call(env, "PUT", "/admin/users", { token: admin, body: { email: "bad@gmail.com", status: "deny" } });
  const tok = await makeToken({ email: "bad@gmail.com" });
  for (let i = 0; i < 2; i++) {
    const r = await call(env, "POST", "/signin", { body: { token: tok } });
    assert.equal(r.status, 200); assert.equal(r.data.allowed, false); assert.equal(r.data.status, "deny");
  }
  assert.equal(JSON.parse(await env.ALERTS.get("acl:bad@gmail.com")).status, "deny");
});

test("signin: invalid token -> 401, nothing written", async () => {
  const env = makeEnv();
  const r = await call(env, "POST", "/signin", { body: { token: await makeToken({ aud: "x" }) } });
  assert.equal(r.status, 401); assert.equal(env.ALERTS.writes, 0);
});

test("signin: DEFAULT_POLICY=deny is allow-list mode", async () => {
  const env = makeEnv({ DEFAULT_POLICY: "deny" });
  let r = await call(env, "POST", "/signin", { body: { token: await makeToken({ email: "u@gmail.com" }) } });
  assert.equal(r.data.allowed, false);
  r = await call(env, "POST", "/signin", { body: { token: await makeToken({ email: ADMIN }) } });
  assert.equal(r.data.allowed, true, "admins always allowed");
  await call(env, "PUT", "/admin/users", { token: await makeToken({ email: ADMIN }), body: { email: "u@gmail.com", status: "allow" } });
  r = await call(env, "POST", "/signin", { body: { token: await makeToken({ email: "u@gmail.com" }) } });
  assert.equal(r.data.allowed, true);
});

// ================= existing alerts routes refuse denied users =================
async function sessionFor(env, email) {
  const r = await call(env, "POST", "/session", { body: { token: await makeToken({ email }) } });
  return r;
}

test("alerts: allowed user can create/list; denied user gets 403 everywhere", async () => {
  const env = makeEnv();
  const s = await sessionFor(env, "x@gmail.com");
  assert.equal(s.status, 200);
  const st = s.data.sessionToken;
  let r = await call(env, "POST", "/alerts", { token: st, body: { index: "n50", metric: "pe", direction: "above", threshold: 25 } });
  assert.equal(r.status, 200);
  assert.equal((await call(env, "GET", "/alerts/all", { token: st })).data.alerts.length, 1);

  await call(env, "PUT", "/admin/users", { token: await makeToken({ email: ADMIN }), body: { email: "x@gmail.com", status: "deny" } });
  // existing 30-day session token is refused on every alerts route
  for (const [m, p, body] of [["GET", "/alerts?index=n50"], ["GET", "/alerts/all"],
    ["POST", "/alerts", { index: "n50", metric: "pe", direction: "below", threshold: 10 }],
    ["DELETE", "/alerts?index=n50&id=whatever"]]) {
    r = await call(env, m, p, { token: st, body });
    assert.equal(r.status, 403, `${m} ${p}`);
    assert.equal(r.data.error, "access denied");
    assert.match(r.data.message, /not allowed/);
  }
  // and a fresh session is refused too
  assert.equal((await sessionFor(env, "x@gmail.com")).status, 403);
  // evaluator export skips the denied user's alerts
  r = await call(env, "GET", "/admin/export", { origin: null, headers: { "X-Admin-Key": "test-admin-key" } });
  assert.equal(r.status, 200);
  assert.equal(r.data.alerts.length, 0); assert.equal(r.data.skippedDeniedUsers, 1);
  // re-allow restores access
  await call(env, "PUT", "/admin/users", { token: await makeToken({ email: ADMIN }), body: { email: "x@gmail.com", status: "allow" } });
  assert.equal((await call(env, "GET", "/alerts/all", { token: st })).status, 200);
  r = await call(env, "GET", "/admin/export", { origin: null, headers: { "X-Admin-Key": "test-admin-key" } });
  assert.equal(r.data.alerts.length, 1);
});

test("admin/export: wrong key -> 403", async () => {
  const r = await call(makeEnv(), "GET", "/admin/export", { headers: { "X-Admin-Key": "nope" } });
  assert.equal(r.status, 403);
});

// ================= CORS =================
test("cors: site origins and localhost reflected; others get no ACAO", async () => {
  const env = makeEnv();
  for (const o of ["https://indexscope.in", "https://www.indexscope.in", "http://localhost:8000", "http://127.0.0.1:5500"]) {
    const r = await call(env, "OPTIONS", "/admin/users", { origin: o });
    assert.equal(r.status, 204);
    assert.equal(r.headers.get("Access-Control-Allow-Origin"), o);
    assert.match(r.headers.get("Access-Control-Allow-Methods"), /PUT/);
  }
  for (const o of ["https://evil.example", "https://indexscope.in.evil.example", "http://localhost.evil.example"]) {
    const r = await call(env, "POST", "/admin/verify", { origin: o, body: { token: await makeToken() } });
    assert.equal(r.headers.get("Access-Control-Allow-Origin"), null, o);
  }
  const r = await call(env, "POST", "/signin", { origin: "https://www.indexscope.in", body: { token: await makeToken() } });
  assert.equal(r.headers.get("Access-Control-Allow-Origin"), "https://www.indexscope.in");
  assert.equal(r.headers.get("Vary"), "Origin");
});

test("cors: public live proxy preflight stays open", async () => {
  const r = await call(makeEnv(), "OPTIONS", "/live", { origin: "https://anything.example" });
  assert.equal(r.headers.get("Access-Control-Allow-Origin"), "*");
});

test("jwks is cached across verifications", async () => {
  const before = jwksFetches;
  const env = makeEnv();
  for (let i = 0; i < 3; i++) await call(env, "POST", "/admin/verify", { body: { token: await makeToken() } });
  assert.equal(jwksFetches, before);
});
