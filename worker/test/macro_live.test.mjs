import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/worker.js";

function setup(mode) {
  const store = new Map();
  globalThis.caches = { default: { match: async k => store.get(k.url)?.clone(), put: async (k, r) => store.set(k.url, r) } };
  let calls = 0;
  globalThis.fetch = async (u) => {
    calls++;
    const sym = decodeURIComponent(String(u).split("/chart/")[1].split("?")[0]);
    if (mode === "fail" || (mode === "partial" && sym === "BZ=F")) return new Response("x", { status: 429 });
    const price = sym === "INR=X" ? 93.9 : (mode === "junk" ? 5 : 98.59);
    return new Response(JSON.stringify({ chart: { result: [{ meta: { regularMarketPrice: price, regularMarketTime: 1793965000 } }] } }));
  };
  return () => calls;
}
const get = () => worker.fetch(new Request("https://x/macro-live"), {});

test("macro-live returns both quotes with open CORS, then serves from cache", async () => {
  const calls = setup("ok");
  let r = await get(), b = await r.json();
  assert.equal(r.status, 200);
  assert.equal(b.quotes.usdinr.last, 93.9);
  assert.equal(b.quotes.brent.last, 98.59);
  assert.equal(r.headers.get("access-control-allow-origin"), "*");
  const n = calls();
  r = await get();
  assert.equal(r.headers.get("X-Cache"), "HIT");
  assert.equal(calls(), n);
});

test("one failing symbol is dropped, the other stays", async () => {
  setup("partial");
  const b = await (await get()).json();
  assert.deepEqual(Object.keys(b.quotes), ["usdinr"]);
});

test("out-of-range price is rejected", async () => {
  setup("junk");
  const b = await (await get()).json();
  assert.deepEqual(Object.keys(b.quotes), ["usdinr"]);
});

test("total failure returns 502 and is not cached", async () => {
  setup("fail");
  const r = await get();
  assert.equal(r.status, 502);
  assert.equal(r.headers.get("cache-control"), "no-store");
});
