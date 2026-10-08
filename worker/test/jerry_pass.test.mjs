// Offline tests for the Jerry service pass in worker/src/worker.js.
// Run from the repo root:  node --test worker/test/jerry_pass.test.mjs
// No network: Google JWKS and status.json are mocked, KV is in memory.
// The pass values below are test fixtures, not real secrets.
import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";

const worker = (await import(new URL("../src/worker.js", import.meta.url))).default;

const CLIENT_ID = "583500951310-6dapvdgbe6je6k6mi87qn2jt3ori90cj.apps.googleusercontent.com";
const ADMIN = "motiwalatanmay0@gmail.com";
const KID = "test-kid-pass";
const PASS = "test-pass-NOT-REAL-0123456789abcdefABCDEF==";
const STATUS = { overall: "OK", feeds: { n50: { state: "OK" } }, run: { id: "1" } };

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
let ipSeq = 0;
function makeEnv(over = {}) {
  return {
    ALERTS: new FakeKV(), GOOGLE_CLIENT_ID: CLIENT_ID, ADMIN_EMAILS: ADMIN,
    DEFAULT_POLICY: "allow", SIGNIN_MIN_INTERVAL_SEC: "600",
    JWT_SECRET: "test-secret-not-real", ADMIN_KEY: "test-admin-key",
    JERRY_TOKEN: PASS, PASS_RATE_PER_MIN: "20",
    STATUS_URL: "https://status.example/data/status.json", ...over,
  };
}

const b64u = (buf) => Buffer.from(buf).toString("base64url");
const algo = { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" };
const good = await webcrypto.subtle.generateKey(algo, true, ["sign", "verify"]);
const pubJwk = { ...(await webcrypto.subtle.exportKey("jwk", good.publicKey)), kid: KID, alg: "RS256", use: "sig" };
async function makeToken(claims = {}) {
  const now = Math.floor(Date.now() / 1000);
  const payload = { iss: "https://accounts.google.com", aud: CLIENT_ID, sub: "1", email: "user@gmail.com",
    email_verified: true, name: "Test User", iat: now - 10, exp: now + 3600, ...claims };
  const head = b64u(JSON.stringify({ alg: "RS256", kid: KID, typ: "JWT" }));
  const body = b64u(JSON.stringify(payload));
  const sig = await webcrypto.subtle.sign("RSASSA-PKCS1-v1_5", good.privateKey, new TextEncoder().encode(head + "." + body));
  return head + "." + body + "." + b64u(sig);
}

let statusMode = "ok";
const seenUrls = [];
globalThis.fetch = async (input) => {
  const url = typeof input === "string" ? input : input.url;
  seenUrls.push(url);
  if (url.startsWith("https://www.googleapis.com/oauth2/v3/certs")) {
    return new Response(JSON.stringify({ keys: [pubJwk] }), { headers: { "Cache-Control": "max-age=3600" } });
  }
  if (url === "https://status.example/data/status.json") {
    if (statusMode === "down") throw new Error("connect ECONNREFUSED");
    return new Response(JSON.stringify(STATUS), { headers: { "Content-Type": "application/json" } });
  }
  throw new Error("unexpected network call in test: " + url);
};

// Each call gets its own client IP unless one is given, so the limiter does
// not couple unrelated tests.
async function call(env, method, path, { body, token, ip, headers = {} } = {}) {
  const h = { "CF-Connecting-IP": ip || `10.0.0.${++ipSeq % 250}`, ...headers };
  if (token) h.Authorization = "Bearer " + token;
  if (body !== undefined) h["Content-Type"] = "application/json";
  const res = await worker.fetch(new Request("https://w.example" + path, {
    method, headers: h, body: body !== undefined ? JSON.stringify(body) : undefined,
  }), env);
  const text = await res.text();
  let data; try { data = JSON.parse(text); } catch (e) { data = text; }
  return { status: res.status, data, text };
}

async function seed(env) {
  // two sign-ins and one deny, via the real routes
  await call(env, "POST", "/signin", { body: { token: await makeToken({ email: "a@gmail.com" }) } });
  await call(env, "POST", "/signin", { body: { token: await makeToken({ email: "b@gmail.com" }) } });
  const adminTok = await makeToken({ email: ADMIN });
  await call(env, "PUT", "/admin/users", { token: adminTok, body: { email: "b@gmail.com", status: "deny" } });
}

test("pass: valid pass reads /admin/users", async () => {
  const env = makeEnv(); await seed(env);
  const r = await call(env, "GET", "/admin/users", { token: PASS });
  assert.equal(r.status, 200);
  const emails = r.data.users.map(u => u.email).sort();
  assert.deepEqual(emails, ["a@gmail.com", "b@gmail.com", ADMIN].sort());
  assert.equal(r.data.users.find(u => u.email === "b@gmail.com").allowed, false);
});

test("pass: valid pass reads /admin/health (status.json + counts + last sign-ins)", async () => {
  const env = makeEnv(); await seed(env);
  statusMode = "ok";
  const r = await call(env, "GET", "/admin/health", { token: PASS });
  assert.equal(r.status, 200);
  assert.deepEqual(r.data.status, STATUS);
  assert.equal(r.data.via, "jerry-pass");
  assert.equal(r.data.users.allowed, 1);
  assert.equal(r.data.users.denied, 1);
  assert.equal(r.data.users.explicitDeny, 1);
  assert.ok(r.data.lastSigninAt);
  assert.equal(r.data.recentSignins.length, 2);
});

test("pass: health still answers when status.json is unreachable", async () => {
  statusMode = "down";
  const r = await call(makeEnv(), "GET", "/admin/health", { token: PASS });
  statusMode = "ok";
  assert.equal(r.status, 200);
  assert.equal(r.data.status, null);
  assert.match(r.data.statusError, /fetch failed/);
});

test("pass: cannot PUT /admin/users (403) and nothing is written", async () => {
  const env = makeEnv();
  const before = env.ALERTS.writes;
  const r = await call(env, "PUT", "/admin/users", { token: PASS, body: { email: "x@gmail.com", status: "deny" } });
  assert.equal(r.status, 403);
  assert.equal(env.ALERTS.writes, before);
  assert.equal(await env.ALERTS.get("acl:x@gmail.com"), null);
});

test("pass: cannot call /signin, /admin/verify, alerts or export", async () => {
  const env = makeEnv();
  assert.equal((await call(env, "POST", "/signin", { token: PASS, body: {} })).status, 401);
  assert.equal((await call(env, "POST", "/signin", { body: { token: PASS } })).status, 401);
  assert.equal((await call(env, "POST", "/admin/verify", { token: PASS, body: {} })).status, 401);
  assert.equal((await call(env, "GET", "/alerts/all", { token: PASS })).status, 401);
  assert.equal((await call(env, "GET", "/alerts?index=n50", { token: PASS })).status, 401);
  assert.equal((await call(env, "GET", "/admin/export", { token: PASS })).status, 403);
  assert.equal(env.ALERTS.writes, 0);
});

test("pass: wrong token gives the same 401 body as no token", async () => {
  const env = makeEnv();
  for (const path of ["/admin/users", "/admin/health"]) {
    const none = await call(env, "GET", path);
    const wrong = await call(env, "GET", path, { token: PASS.slice(0, -3) + "xyz" });
    const short = await call(env, "GET", path, { token: "x" });
    assert.equal(none.status, 401);
    assert.equal(wrong.status, 401); assert.equal(wrong.text, none.text);
    assert.equal(short.status, 401); assert.equal(short.text, none.text);
  }
  // wrong pass on PUT is also the plain 401, not the read-only 403
  const put = await call(env, "PUT", "/admin/users", { token: "nope-nope-nope", body: { email: "x@gmail.com", status: "deny" } });
  assert.equal(put.status, 401);
});

test("pass: unset or too-short JERRY_TOKEN disables the pass (fail closed)", async () => {
  for (const over of [{ JERRY_TOKEN: undefined }, { JERRY_TOKEN: "" }, { JERRY_TOKEN: "short" }]) {
    const env = makeEnv(over);
    const none = await call(env, "GET", "/admin/users");
    const tok = over.JERRY_TOKEN || PASS;
    const r = await call(env, "GET", "/admin/users", { token: tok });
    assert.equal(r.status, 401); assert.equal(r.text, none.text);
    assert.equal((await call(env, "GET", "/admin/health", { token: tok })).status, 401);
  }
});

test("pass: per-IP per-minute limit returns 429, other IPs unaffected", async () => {
  const env = makeEnv({ PASS_RATE_PER_MIN: "3" });
  const ip = "192.0.2.77";
  const codes = [];
  for (let i = 0; i < 5; i++) codes.push((await call(env, "GET", "/admin/users", { token: "wrong-guess-" + i, ip })).status);
  assert.deepEqual(codes, [401, 401, 401, 429, 429]);
  // even the right pass is held off from this IP for the rest of the minute
  assert.equal((await call(env, "GET", "/admin/users", { token: PASS, ip })).status, 429);
  assert.equal((await call(env, "GET", "/admin/users", { token: PASS, ip: "192.0.2.78" })).status, 200);
});

test("pass: responses never echo the token", async () => {
  const env = makeEnv(); await seed(env);
  for (const [m, p] of [["GET", "/admin/users"], ["GET", "/admin/health"], ["PUT", "/admin/users"]]) {
    const r = await call(env, m, p, { token: PASS, body: m === "PUT" ? { email: "q@gmail.com", status: "allow" } : undefined });
    assert.ok(!r.text.includes(PASS), `${m} ${p} leaked the pass`);
  }
});

test("admin Google login still works with the pass enabled (GET, PUT, health)", async () => {
  const env = makeEnv(); await seed(env);
  const tok = await makeToken({ email: ADMIN });
  assert.equal((await call(env, "GET", "/admin/users", { token: tok })).status, 200);
  const put = await call(env, "PUT", "/admin/users", { token: tok, body: { email: "b@gmail.com", status: "allow" } });
  assert.equal(put.status, 200); assert.equal(put.data.user.allowed, true);
  const h = await call(env, "GET", "/admin/health", { token: tok });
  assert.equal(h.status, 200); assert.equal(h.data.via, "admin");
  // a non-admin Google account still cannot read health
  assert.equal((await call(env, "GET", "/admin/health", { token: await makeToken({ email: "a@gmail.com" }) })).status, 403);
});

test("pass: POST /session with the pass is refused without forwarding it to Google", async () => {
  const env = makeEnv();
  const before = seenUrls.length;
  const r = await call(env, "POST", "/session", { body: { token: PASS } });
  assert.equal(r.status, 401);
  const calls = seenUrls.slice(before);
  assert.ok(!calls.some(u => u.includes("userinfo")), "pass was sent to Google userinfo: " + calls.join(","));
  assert.ok(!r.text.includes(PASS));
});

test("pass: other methods on admin routes do nothing", async () => {
  const env = makeEnv();
  for (const [m, p] of [["DELETE", "/admin/users"], ["POST", "/admin/users"], ["PATCH", "/admin/users"],
                        ["PUT", "/admin/health"], ["POST", "/admin/health"], ["DELETE", "/admin/health"]]) {
    const r = await call(env, m, p, { token: PASS, body: m === "DELETE" ? undefined : { email: "x@gmail.com", status: "deny" } });
    assert.equal(r.status, 404, `${m} ${p}`);
  }
  assert.equal(env.ALERTS.writes, 0);
});

test("pass: token never appears in console output or response headers", async () => {
  const env = makeEnv(); await seed(env);
  const logged = [];
  const saved = {};
  for (const k of ["log", "info", "warn", "error", "debug"]) {
    saved[k] = console[k];
    console[k] = (...a) => logged.push(a.map(String).join(" "));
  }
  const heads = [];
  try {
    const reqs = [["GET", "/admin/users"], ["GET", "/admin/health"], ["PUT", "/admin/users"],
                  ["POST", "/admin/verify"], ["POST", "/signin"], ["POST", "/session"], ["GET", "/alerts/all"]];
    for (const [m, p] of reqs) {
      for (const tok of [PASS, PASS + "x"]) {
        const res = await worker.fetch(new Request("https://w.example" + p, {
          method: m,
          headers: { "CF-Connecting-IP": "10.9.9." + (heads.length % 250), Authorization: "Bearer " + tok, "Content-Type": "application/json" },
          body: m === "GET" ? undefined : JSON.stringify({ token: tok, email: "q@gmail.com", status: "allow" }),
        }), env);
        heads.push([...res.headers].map(([k, v]) => k + ": " + v).join("\n"));
        const text = await res.text();
        assert.ok(!text.includes(PASS), `${m} ${p} body leaked the pass`);
      }
    }
  } finally {
    for (const k of Object.keys(saved)) console[k] = saved[k];
  }
  assert.ok(!logged.some(l => l.includes(PASS)), "console output contained the pass");
  assert.ok(!heads.some(h => h.includes(PASS)), "a response header contained the pass");
});
