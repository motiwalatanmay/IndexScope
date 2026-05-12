// Cloudflare Worker: proxies NSE /api/allIndices to return live prices for
// the 4 IndexScope indices. Caches 60s edge-side so traffic bursts don't
// hammer NSE. Browser-callable (CORS open).

const INDEX_MAP = {
  n50:     "NIFTY 50",
  nn50:    "NIFTY NEXT 50",
  nmid150: "NIFTY MIDCAP 150",
  sc250:   "NIFTY SMALLCAP 250",
};

const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
const CACHE_TTL_SECONDS = 60;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS });
    }

    const cache = caches.default;
    const cacheKey = new Request("https://indexscope-cache/live", request);
    let cached = await cache.match(cacheKey);
    if (cached) {
      const fresh = new Response(cached.body, cached);
      Object.entries(CORS).forEach(([k, v]) => fresh.headers.set(k, v));
      fresh.headers.set("X-Cache", "HIT");
      return fresh;
    }

    let body;
    try {
      body = await fetchLive();
    } catch (e) {
      return json({ status: "error", message: String(e) }, 502);
    }

    const resp = json(body, 200, {
      "Cache-Control": `public, max-age=${CACHE_TTL_SECONDS}`,
      "X-Cache": "MISS",
    });
    await cache.put(cacheKey, resp.clone());
    return resp;
  },
};

async function fetchLive() {
  const headers = {
    "User-Agent": UA,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
  };

  // Cookie warmup: NSE blocks /api/* without prior page hit.
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
  if (!apiResp.ok) {
    throw new Error(`NSE returned ${apiResp.status}`);
  }
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

function json(obj, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...CORS,
      ...extraHeaders,
    },
  });
}
