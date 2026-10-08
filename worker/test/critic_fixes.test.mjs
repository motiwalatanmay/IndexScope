// Offline tests for the five worker defects found by the final critic
// (CRITIC3_report.md, Group D). One test group per fix. Run from the repo root:
//   node --test worker/test/*.test.mjs
// Each test file runs in its own process, so this file gets fresh module state
// (JWKS cache and refetch throttle) independent of the other files.
import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";

const SRC_URL = new URL("../src/worker.js", import.meta.url);
const worker = (await import(SRC_URL)).default;

const CLIENT_ID = "583500951310-6dapvdgbe6je6k6mi87qn2jt3ori90cj.apps.googleusercontent.com";
const ADMIN = "motiwalatanmay0@gmail.com";
const KID = "test-kid-1";

// ---------- controllable clock ----------
const realNow = Date.now.bind(Date);
let clockOffsetMs = 0;
Date.now = () => realNow() + clockOffsetMs;

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
    ALERTS: new FakeKV(), GOOGLE_CLIENT_ID: CLIENT_ID, ADMIN_EMAILS: ADMIN,
    DEFAULT_POLICY: "allow", SIGNIN_MIN_INTERVAL_SEC: "600",
    JWT_SECRET: "test-secret-not-real", ADMIN_KEY: "test-admin-key-NOT-REAL", ...over,
  };
}

// ---------- tokens ----------
const b64u = (buf) => Buffer.from(buf).toString("base64url");
const algo = { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" };
const good = await webcrypto.subtle.generateKey(algo, true, ["sign", "verify"]);
const rotated = await webcrypto.subtle.generateKey(algo, true, ["sign", "verify"]);
const jwkOf = async (kp, kid) => ({ ...(await webcrypto.subtle.exportKey("jwk", kp.publicKey)), kid, alg: "RS256", use: "sig" });
const goodJwk = await jwkOf(good, KID);
const rotatedJwk = await jwkOf(rotated, "rotated-kid");

async function makeToken(claims = {}, { key = good.privateKey, kid = KID } = {}) {
  const now = Math.floor(Date.now() / 1000);
  const payload = {
    iss: "https://accounts.google.com", aud: CLIENT_ID, sub: "1234",
    email: "user@gmail.com", email_verified: true, name: "Test User",
    iat: now - 10, exp: now + 3600, ...claims,
  };
  const head = b64u(JSON.stringify({ alg: "RS256", kid, typ: "JWT" }));
  const body = b64u(JSON.stringify(payload));
  const sig = await webcrypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(head + "." + body));
  return head + "." + body + "." + b64u(sig);
}

// ---------- fetch mock: JWKS only; Google tokeninfo/userinfo must never be called ----------
let publishedKeys = [goodJwk];
let jwksFetches = 0;
const seenUrls = [];
globalThis.fetch = async (input) => {
  const url = typeof input === "string" ? input : input.url;
  seenUrls.push(url);
  if (url.startsWith("https://www.googleapis.com/oauth2/v3/certs")) {
    jwksFetches++;
    return new Response(JSON.stringify({ keys: publishedKeys }), {
      headers: { "Content-Type": "application/json", "Cache-Control": "public, max-age=3600" },
    });
  }
  // tokeninfo / userinfo answer "valid" so a regression that calls them would succeed.
  if (url.includes("tokeninfo") || url.includes("userinfo")) {
    return new Response(JSON.stringify({ email: "victim@gmail.com", aud: "other-app", email_verified: "true" }),
      { headers: { "Content-Type": "application/json" } });
  }
  throw new Error("unexpected network call in test: " + url);
};
const googleProfileCalls = () => seenUrls.filter(u => u.includes("tokeninfo") || u.includes("userinfo"));

async function call(env, method, path, { body, token, headers = {} } = {}) {
  const h = { Origin: "https://indexscope.in", ...headers };
  if (token) h.Authorization = "Bearer " + token;
  if (body !== undefined) h["Content-Type"] = "application/json";
  const res = await worker.fetch(new Request("https://indexscope-live.example" + path, {
    method, headers: h, body: body !== undefined ? JSON.stringify(body) : undefined,
  }), env);
  const text = await res.text();
  let data = null; try { data = JSON.parse(text); } catch (e) { data = text; }
  return { status: res.status, data };
}

// ================= Fix 3: unknown-kid JWKS refetch is throttled =================
// Runs first so this process has made no forced refetch yet.
test("fix3: unknown kid refetches Google keys at most once per 5 minutes", async () => {
  const env = makeEnv();
  // Warm the cache with a valid token: exactly one normal fetch.
  let r = await call(env, "POST", "/admin/verify", { body: { token: await makeToken() } });
  assert.equal(r.status, 200);
  assert.equal(jwksFetches, 1);

  // First unknown kid: one forced refetch.
  r = await call(env, "POST", "/admin/verify", { body: { token: await makeToken({}, { kid: "forged-1" }) } });
  assert.equal(r.status, 401);
  assert.equal(r.data.reason, "unknown signing key");
  assert.equal(jwksFetches, 2);

  // A flood of unknown kids inside the window: no further fetches.
  for (let i = 0; i < 25; i++) {
    r = await call(env, "POST", "/admin/verify", { body: { token: await makeToken({}, { kid: "forged-x" + i }) } });
    assert.equal(r.status, 401);
    assert.equal(r.data.reason, "unknown signing key");
  }
  clockOffsetMs = 4 * 60 * 1000 + 59 * 1000;   // 4m59s later: still throttled
  r = await call(env, "POST", "/admin/verify", { body: { token: await makeToken({}, { kid: "forged-2" }) } });
  assert.equal(r.data.reason, "unknown signing key");
  assert.equal(jwksFetches, 2, "refetched inside the 5-minute window");

  // Google rotates keys; after the window a token with the new kid is accepted
  // after exactly one more refetch.
  publishedKeys = [goodJwk, rotatedJwk];
  clockOffsetMs = 5 * 60 * 1000 + 1000;
  r = await call(env, "POST", "/admin/verify", { body: { token: await makeToken({}, { key: rotated.privateKey, kid: "rotated-kid" }) } });
  assert.equal(r.status, 200, JSON.stringify(r.data));
  assert.equal(jwksFetches, 3);
  // The rotated key is now cached: no fetch for the next token signed with it.
  r = await call(env, "POST", "/admin/verify", { body: { token: await makeToken({}, { key: rotated.privateKey, kid: "rotated-kid" }) } });
  assert.equal(r.status, 200);
  assert.equal(jwksFetches, 3);
  clockOffsetMs = 0;
});

// ================= Fix 1: /session takes no OAuth access tokens =================
test("fix1: /session refuses access tokens and never calls Google tokeninfo/userinfo", async () => {
  const env = makeEnv();
  const before = googleProfileCalls().length;
  // Shapes of a Google OAuth access token (opaque, not a 3-part JWT).
  for (const tok of ["ya29.a0AfH6SMBfakeAccessTokenNOTREAL", "ya29_fakeAccessTokenNOTREAL", "x.y"]) {
    const r = await call(env, "POST", "/session", { body: { token: tok } });
    assert.equal(r.status, 401, tok);
    assert.equal(r.data.error, "invalid Google token");
    assert.ok(!r.data.sessionToken);
  }
  // An ID token minted for another app (wrong aud) is also refused.
  const other = await call(env, "POST", "/session", { body: { token: await makeToken({ aud: "other-app.apps.googleusercontent.com" }) } });
  assert.equal(other.status, 401);
  assert.equal(other.data.reason, "wrong audience");
  // A good ID token still works, verified locally.
  const ok = await call(env, "POST", "/session", { body: { token: await makeToken() } });
  assert.equal(ok.status, 200);
  assert.ok(ok.data.sessionToken);
  assert.equal(ok.data.email, "user@gmail.com");
  assert.equal(googleProfileCalls().length, before, "worker called tokeninfo/userinfo: " + googleProfileCalls().join(","));
  // The session works on /alerts.
  const a = await call(env, "GET", "/alerts", { token: ok.data.sessionToken });
  assert.equal(a.status, 200);
});

// ================= Fix 2: iss and email_verified on every token route =================
test("fix2: /session, /signin and /admin/verify all require iss and email_verified", async () => {
  const cases = [
    [{ iss: "https://evil.example" }, "wrong issuer"],
    [{ iss: "accounts.google.com.evil.example" }, "wrong issuer"],
    [{ iss: undefined }, "wrong issuer"],
    [{ email_verified: false }, "email not verified"],
    [{ email_verified: "false" }, "email not verified"],
    [{ email_verified: undefined }, "email not verified"],
  ];
  for (const path of ["/session", "/signin", "/admin/verify"]) {
    for (const [claims, reason] of cases) {
      const env = makeEnv();
      const r = await call(env, "POST", path, { body: { token: await makeToken(claims) } });
      assert.equal(r.status, 401, path + " " + JSON.stringify(claims));
      assert.equal(r.data.reason, reason, path + " " + JSON.stringify(claims));
      assert.equal(env.ALERTS.writes, 0);
    }
    // Both accepted issuer spellings pass.
    for (const iss of ["accounts.google.com", "https://accounts.google.com"]) {
      const r = await call(makeEnv(), "POST", path, { body: { token: await makeToken({ iss }) } });
      assert.equal(r.status, 200, path + " " + iss);
    }
  }
});

// ================= Fix 4: email length cap (254) =================
test("fix4: emails over 254 characters are refused on every route that takes one", async () => {
  const at = "@gmail.com";
  const e254 = "a".repeat(254 - at.length) + at;
  const e255 = "a".repeat(255 - at.length) + at;
  assert.equal(e254.length, 254);
  assert.equal(e255.length, 255);

  // Token claims: /session, /signin, /admin/verify, and the admin bearer on /admin/users.
  for (const path of ["/session", "/signin", "/admin/verify"]) {
    const env = makeEnv();
    const r = await call(env, "POST", path, { body: { token: await makeToken({ email: e255 }) } });
    assert.equal(r.status, 401, path);
    assert.equal(r.data.reason, "bad email", path);
    assert.equal(env.ALERTS.writes, 0, path);
    const ok = await call(makeEnv(), "POST", path, { body: { token: await makeToken({ email: e254 }) } });
    assert.equal(ok.status, 200, path + " (254 must pass)");
  }
  const longAdmin = await call(makeEnv(), "GET", "/admin/users", { token: await makeToken({ email: e255 }) });
  assert.equal(longAdmin.status, 401);

  // PUT /admin/users body.
  const env = makeEnv();
  const adminTok = await makeToken({ email: ADMIN });
  for (const bad of [e255, " " + "a".repeat(400) + at, "x".repeat(100000) + at]) {
    const r = await call(env, "PUT", "/admin/users", { token: adminTok, body: { email: bad, status: "deny" } });
    assert.equal(r.status, 400);
    assert.equal(r.data.error, "bad email");
  }
  assert.equal(env.ALERTS.writes, 0);
  const ok = await call(env, "PUT", "/admin/users", { token: adminTok, body: { email: e254, status: "deny" } });
  assert.equal(ok.status, 200);
  assert.equal(env.ALERTS.writes, 1);
});

// ================= Fix 5: X-Admin-Key constant-time compare =================
test("fix5: /admin/export X-Admin-Key: right key only, fail closed, constant-time compare", async () => {
  const env = makeEnv();
  const key = env.ADMIN_KEY;
  const ok = await call(env, "GET", "/admin/export", { headers: { "X-Admin-Key": key } });
  assert.equal(ok.status, 200);
  for (const wrong of ["", key.slice(0, -1), key + "x", key.toUpperCase(), key.slice(0, -1) + "Y"]) {
    const r = await call(env, "GET", "/admin/export", { headers: wrong ? { "X-Admin-Key": wrong } : {} });
    assert.equal(r.status, 403, JSON.stringify(wrong));
  }
  // Fail closed when ADMIN_KEY is unset or empty, even with an empty header.
  for (const unset of [undefined, ""]) {
    const e2 = makeEnv({ ADMIN_KEY: unset });
    for (const h of [{}, { "X-Admin-Key": "" }, { "X-Admin-Key": "undefined" }]) {
      const r = await call(e2, "GET", "/admin/export", { headers: h });
      assert.equal(r.status, 403);
    }
  }
  // Static check: the key is compared through timingSafeEqualStr, never with ===/!==.
  const src = readFileSync(SRC_URL, "utf8");
  const fn = src.slice(src.indexOf("async function adminExportHandler"), src.indexOf("async function adminExportHandler") + 600);
  assert.match(fn, /timingSafeEqualStr\(request\.headers\.get\("X-Admin-Key"\)/);
  assert.doesNotMatch(src, /X-Admin-Key"\)\s*[!=]==/);
  assert.doesNotMatch(src, /[!=]==\s*env\.ADMIN_KEY/);
});
