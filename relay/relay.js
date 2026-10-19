// The relay: a Cloudflare Worker that fetches what a visitor's network will not let them fetch.
//
// Some networks block Mojang outright (DNS poisoned, or an allowlist), and every browser refuses to
// fetch some of it from a page in the first place. This Worker maps a prefix back onto the upstream
// URL, streams the bytes back, and caches them at Cloudflare's edge, so a launch costs the origin
// once per file rather than once per visitor.
//
// It runs nothing and holds nothing of anybody's game: it is a proxy for public files.
//
//   deploy:  npx wrangler deploy relay/relay.js --name webmc-relay --compatibility-date 2026-01-01
//   use:     webmc_helper.py --relay https://webmc-relay.<your-subdomain>.workers.dev
//
// Prefixes must match RELAY_PREFIXES in helper/webmc_helper.py.

const UPSTREAM = {
  "piston-meta": "https://piston-meta.mojang.com",
  "piston-data": "https://piston-data.mojang.com",
  "resources": "https://resources.download.minecraft.net",
  "libraries": "https://libraries.minecraft.net",
  "fabric-maven": "https://maven.fabricmc.net",
  "fabric-meta": "https://meta.fabricmc.net",
  "forge-maven": "https://maven.minecraftforge.net",
  "neoforge-maven": "https://maven.neoforged.net/releases",
  "modrinth": "https://api.modrinth.com",
  "adoptium": "https://api.adoptium.net",
};

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
  "Access-Control-Allow-Headers": "*",
};

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    if (url.pathname === "/" || url.pathname === "/health") {
      return Response.json({ ok: true, service: "webmc-relay", prefixes: Object.keys(UPSTREAM) }, { headers: CORS });
    }
    const [, prefix, ...rest] = url.pathname.split("/");
    const upstreamBase = UPSTREAM[prefix];
    if (!upstreamBase) {
      return Response.json({ error: "unknown prefix", prefix, known: Object.keys(UPSTREAM) },
                           { status: 404, headers: CORS });
    }
    const target = upstreamBase + "/" + rest.join("/") + url.search;

    // Cache at the edge: these files are immutable and identical for every visitor.
    const cache = caches.default;
    const cacheKey = new Request(target, { method: "GET" });
    let response = await cache.match(cacheKey);
    if (!response) {
      response = await fetch(target, {
        method: request.method === "HEAD" ? "GET" : request.method,
        headers: { "User-Agent": request.headers.get("User-Agent") || "webmc-relay" },
      });
      if (response.ok && request.method === "GET") {
        const copy = new Response(response.clone().body, response);
        copy.headers.set("Cache-Control", "public, max-age=31536000, immutable");
        ctx.waitUntil(cache.put(cacheKey, copy));
      }
    }
    const out = new Response(response.body, response);
    for (const [k, v] of Object.entries(CORS)) out.headers.set(k, v);
    out.headers.set("X-Relayed-From", upstreamBase);
    return out;
  },
};
