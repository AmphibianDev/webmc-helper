// The WebMC relay: a Cloudflare Worker that fetches, and dials, on behalf of a visitor.
//
// Two jobs, one deployment:
//
//   1. FILES. Some networks block Mojang outright, and a browser refuses some of it anyway. Any
//      request to /<prefix>/<path> is mapped back onto the real host, streamed to the caller, and
//      cached at Cloudflare's edge, so the origin is hit once per file rather than once per visitor.
//
//   2. TCP, inside a WebSocket. A network that lets nothing out but HTTPS cannot reach a Minecraft
//      server, because the game wants a raw port. This speaks the same "goldproxy.v1" dialect the
//      repository's own gateway documents, so the helper's server tunnel and its SOCKS5 proxy both
//      work through it unchanged:
//
//          POST /v1/token    {"host":…,"port":…}      -> {"token":…,"expiresAt":…}
//          GET  /v1/connect  (WebSocket, subprotocol "goldproxy.v1")
//                            {"t":"open","v":1,"token":…,"host":…,"port":…}
//                            <- {"t":"ready","ip":…,"port":…,"window":…}
//                            then binary frames are raw TCP bytes, and text frames are
//                            {"t":"ack","n"} (cumulative, flow control), {"t":"ping"/"pong","id"},
//                            {"t":"error",…}, {"t":"end",…}
//
// This runs nothing of anybody's game and holds no session: it is a proxy for public files and a dial
// for a port the visitor asked for. It refuses to be a port scanner, which is why a session has to
// begin with a Minecraft handshake, why private addresses are refused unless an operator opts in, and
// why a token is signed, short-lived and scoped to one destination.
//
// Deploy:
//   npx wrangler deploy relay/relay.js --name webmc-relay --compatibility-date 2026-01-01
//
// Optional variables (wrangler.toml or the dashboard):
//   RELAY_SECRET=…             signing key for tokens (default: derived from the script, so set it)
//   RELAY_ALLOW_PRIVATE=1      allow LAN/loopback targets (default 0: refuses them)
//   RELAY_ALLOW_ANY_PORT=1     stop enforcing the port allowlist (default 0: it is enforced)
//   RELAY_ALLOWED_PORTS=25565,25566,19132   ports a session may dial to (default below)
//   RELAY_MAX_SECONDS=1800     longest a single session may live

import { connect } from "cloudflare:sockets";

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
  "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
  "Access-Control-Allow-Headers": "*",
};

const DEFAULT_PORTS = [25565, 25566, 25567, 25568, 25575, 25585, 19132];
const WINDOW_BYTES = 512 * 1024;
const CHUNK = 32 * 1024;
const MAX_BYTES = 512 * 1024 * 1024;

function json(body, init = {}) {
  return new Response(JSON.stringify(body, null, 1),
                      { ...init, headers: { "Content-Type": "application/json", ...CORS, ...(init.headers || {}) } });
}

// ------------------------------------------------------------------ what may be dialled

function isPrivateHost(host) {
  const v4 = /^(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(host);
  if (v4) {
    const [a, b] = [Number(v4[1]), Number(v4[2])];
    return a === 0 || a === 10 || a === 127 || (a === 169 && b === 254)
        || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168)
        || (a === 100 && b >= 64 && b <= 127);
  }
  const lower = host.toLowerCase().replace(/^\[|\]$/g, "");
  if (lower === "localhost" || lower.endsWith(".local") || lower.endsWith(".internal")) return true;
  if (/^f[cd][0-9a-f]{2}:/.test(lower) || /^fe80:/.test(lower) || lower === "::1") return true;
  return false;
}

function allowedPorts(env) {
  // The allowlist is the allowlist: it is enforced whether or not private addresses are allowed,
  // because "the visitor asked for a LAN address" and "the visitor asked for port 22" are different
  // requests. An operator who really wants any port sets RELAY_ALLOW_ANY_PORT=1.
  if (env.RELAY_ALLOW_ANY_PORT === "1") return null;
  const fromEnv = (env.RELAY_ALLOWED_PORTS || "").split(",").map((s) => Number(s.trim())).filter(Boolean);
  return new Set(fromEnv.length ? fromEnv : DEFAULT_PORTS);
}

function portAllowed(env, port) {
  const ports = allowedPorts(env);
  return ports === null || ports.has(Number(port));
}

// ------------------------------------------------------------------ tokens

const enc = new TextEncoder();

function b64url(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function key(env) {
  const secret = env.RELAY_SECRET || "webmc-relay-development-secret";
  return crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" },
                                 false, ["sign", "verify"]);
}

async function makeToken(env, host, port, ttlSeconds = 120) {
  const expiresAt = Math.floor(Date.now() / 1000) + ttlSeconds;
  const payload = b64url(enc.encode(JSON.stringify({ host, port, expiresAt })));
  const sig = await crypto.subtle.sign("HMAC", await key(env), enc.encode(payload));
  return { token: payload + "." + b64url(sig), expiresAt };
}

async function readToken(env, token) {
  const [payload, sig] = String(token || "").split(".");
  if (!payload || !sig) return null;
  const expected = await crypto.subtle.sign("HMAC", await key(env), enc.encode(payload));
  if (b64url(expected) !== sig) return null;
  try {
    const data = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(payload.replace(/-/g, "+").replace(/_/g, "/")),
                                                                   (c) => c.charCodeAt(0))));
    if (!data.expiresAt || data.expiresAt < Math.floor(Date.now() / 1000)) return null;
    return data;
  } catch { return null; }
}

// ------------------------------------------------------------------ the handshake guard

// A relay that dialled any port for any bytes would be a port scanner with a nicer interface, so a
// session has to start the way a Minecraft client starts: a VarInt length, then packet id 0.
function concat(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0); out.set(b, a.length);
  return out;
}

function looksLikeMinecraftHandshake(bytes) {
  if (!bytes || bytes.length < 3) return false;
  let i = 0, length = 0, shift = 0;
  while (i < bytes.length && i < 5) {
    const b = bytes[i++];
    length |= (b & 0x7f) << shift;
    shift += 7;
    if (!(b & 0x80)) break;
  }
  if (length < 2 || length > 4096) return false;
  if (i >= bytes.length) return false;
  const id = bytes[i];
  return id === 0x00;                 // 0x00 = handshake in every version that has one
}

// ------------------------------------------------------------------ the TCP session

async function dial(env, { host, port, token }) {
  const data = await readToken(env, token);
  if (!data || data.host !== host || Number(data.port) !== Number(port)) {
    return { error: { t: "error", code: "UNAUTHORIZED", message: "that token is not for this address" } };
  }
  if (!portAllowed(env, port)) {
    return { error: { t: "error", code: "PORT_BLOCKED", message: `port ${port} is not one this relay dials` } };
  }
  if (isPrivateHost(host) && env.RELAY_ALLOW_PRIVATE !== "1") {
    return { error: { t: "error", code: "ADDRESS_BLOCKED", message: "that address is private or reserved" } };
  }
  const started = Date.now();
  const socket = connect({ hostname: host, port: Number(port) });
  try {
    await socket.opened;
  } catch (err) {
    return { error: { t: "error", code: "CONNECT_FAILED", message: String(err && err.message || err) } };
  }
  return { socket, connectMs: Date.now() - started };
}

async function serveTunnel(request, env) {
  if (request.headers.get("Upgrade") !== "websocket") {
    return json({ error: "this endpoint wants a WebSocket upgrade" }, { status: 426 });
  }
  if (!(request.headers.get("Sec-WebSocket-Protocol") || "").split(",")
        .map((s) => s.trim()).includes("goldproxy.v1")) {
    return new Response("expected the goldproxy.v1 subprotocol", { status: 400, headers: CORS });
  }
  const pair = new WebSocketPair();
  const [client, server] = Object.values(pair);
  server.accept();

  let socket = null, writer = null, finished = false, received = 0, written = 0;
  let checkedHandshake = false, heardAnything = false, silent = null;
  const limit = Number(env.RELAY_MAX_SECONDS || 1800) * 1000;
  const deadline = setTimeout(() => finish("IDLE", "the session reached its time limit"), limit);

  let handshakeBuffer = new Uint8Array(0);

  // The first bytes can arrive split across frames, so wait for enough of them before judging:
  // refusing early is how you break a perfectly good session on a slow network. Eight bytes is
  // enough to be sure, since a handshake opens with a length VarInt of at most five bytes and a
  // packet id of one.
  async function checkHandshake(bytes) {
    handshakeBuffer = concat(handshakeBuffer, bytes);
    if (looksLikeMinecraftHandshake(handshakeBuffer)) { checkedHandshake = true; return; }
    if (handshakeBuffer.length >= 8) {
      finish("NOT_MINECRAFT", "a session has to begin with a Minecraft handshake");
    }
  }

  function finish(code, message) {
    if (finished) return;
    finished = true;
    clearTimeout(deadline);
    clearTimeout(silent);
    try { server.send(JSON.stringify({ t: "end", code, message })); } catch {}
    try { writer && writer.close(); } catch {}
    try { socket && socket.close(); } catch {}
    try { server.close(); } catch {}
  }

  async function toSocket(bytes) {
    if (!checkedHandshake) {
      // The first bytes can arrive split across frames, so wait for enough of them before judging:
      // refusing early is how you break a perfectly good session on a slow network.
      await checkHandshake(bytes);
      if (finished) return;
    }
    writer = writer || socket.writable.getWriter();
    written += bytes.length;
    if (written > MAX_BYTES) { finish("MESSAGE_TOO_LARGE", "this session has moved too much data"); return; }
    await writer.write(bytes);
  }

  server.addEventListener("message", async (event) => {
    if (finished) return;
    if (typeof event.data === "string") {
      let message = {};
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.t === "open") {
        const result = await dial(env, {
          host: message.host, port: message.port, token: message.token,
        });
        if (result.error) {
          try { server.send(JSON.stringify(result.error)); } catch {}
          finish("REFUSED", result.error.message);
          return;
        }
        socket = result.socket;
        server.send(JSON.stringify({ t: "ready", ip: message.host, port: Number(message.port),
                                     connectMs: result.connectMs, window: WINDOW_BYTES }));
        silent = setTimeout(() => {
          if (!heardAnything) {
            try { server.send(JSON.stringify({ t: "error", code: "NO_DATA",
                                               message: "the server accepted the connection but sent nothing" })); } catch {}
            finish("IDLE", "no data from the target");
          }
        }, 20000);
        // server -> client
        (async () => {
          const reader = socket.readable.getReader();
          try {
            for (;;) {
              const { value, done } = await reader.read();
              if (done) break;
              heardAnything = true;
              for (let off = 0; off < value.length; off += CHUNK) {
                server.send(value.subarray(off, off + CHUNK));
                received += Math.min(CHUNK, value.length - off);
              }
            }
          } catch (err) {
            try { server.send(JSON.stringify({ t: "error", code: "STREAM", message: String(err && err.message || err) })); } catch {}
          }
          finish("END", "the server closed the connection");
        })();
      } else if (message.t === "ack") {
        acked = Number(message.n) || 0;
      } else if (message.t === "ping") {
        try { server.send(JSON.stringify({ t: "pong", id: message.id })); } catch {}
      }
      return;
    }
    try { await toSocket(new Uint8Array(event.data)); }
    catch (err) { finish("CONNECT_FAILED", String(err && err.message || err)); }
  });

  server.addEventListener("close", () => finish("END", "closed"));
  return new Response(null, { status: 101, webSocket: client,
                              headers: { "Sec-WebSocket-Protocol": "goldproxy.v1" } });
}

// ------------------------------------------------------------------ the file relay

async function serveFile(request, env, ctx, prefix, rest) {
  const base = UPSTREAM[prefix];
  if (!base) {
    return json({ error: "unknown prefix", prefix, known: Object.keys(UPSTREAM) }, { status: 404 });
  }
  const url = new URL(request.url);
  const target = base + "/" + rest.join("/") + url.search;
  const cache = caches.default;
  const cacheKey = new Request(target, { method: "GET" });
  let response = request.method === "GET" ? await cache.match(cacheKey) : null;
  if (!response) {
    // only the hosts this relay exists for: an open fetcher would be somebody else's proxy
    try {
      response = await fetch(target, {
        method: request.method === "HEAD" ? "GET" : request.method,
        headers: { "User-Agent": request.headers.get("User-Agent") || "webmc-relay" },
      });
    } catch (err) {
      return json({ error: "UPSTREAM_UNREACHABLE", upstream: base, path: rest.join("/"),
                    message: String(err && err.message || err) }, { status: 502 });
    }
    if (response.status >= 500) {
      return json({ error: "UPSTREAM_ERROR", upstream: base, status: response.status },
                  { status: 502 });
    }
    if (response.ok && request.method === "GET") {
      const copy = new Response(response.clone().body, response);
      copy.headers.set("Cache-Control", "public, max-age=31536000, immutable");
      ctx.waitUntil(cache.put(cacheKey, copy));
    }
  }
  const out = new Response(response.body, response);
  for (const [k, v] of Object.entries(CORS)) out.headers.set(k, v);
  out.headers.set("X-Relayed-From", base);
  return out;
}

// ------------------------------------------------------------------ entry

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });

    if (url.pathname === "/" || url.pathname === "/health") {
      return json({
        ok: true,
        service: "webmc-relay",
        files: Object.keys(UPSTREAM),
        tunnel: true,
        protocol: "goldproxy.v1",
        ports: allowedPorts(env) === null ? "any (RELAY_ALLOW_ANY_PORT=1)" : [...allowedPorts(env)],
        privateTargets: env.RELAY_ALLOW_PRIVATE === "1",
      });
    }

    if (url.pathname === "/v1/token") {
      if (request.method !== "POST") return json({ error: "POST {host,port}" }, { status: 405 });
      let body = {};
      try { body = await request.json(); } catch { return json({ error: "expected JSON" }, { status: 400 }); }
      const host = String(body.host || "").trim();
      const port = Number(body.port);
      if (!host || !port) return json({ error: "host and port are required" }, { status: 400 });
      if (isPrivateHost(host) && env.RELAY_ALLOW_PRIVATE !== "1") {
        return json({ error: "ADDRESS_BLOCKED", message: "that address is in a private or reserved network" },
                    { status: 403 });
      }
      if (!portAllowed(env, port)) {
        return json({ error: "PORT_BLOCKED", message: `port ${port} is not one this relay dials` },
                    { status: 403 });
      }
      const { token, expiresAt } = await makeToken(env, host, port);
      return json({ token, expiresAt });
    }

    if (url.pathname === "/v1/connect") return serveTunnel(request, env);

    const [, prefix, ...rest] = url.pathname.split("/");
    if (!prefix) return json({ error: "nothing here" }, { status: 404 });
    return serveFile(request, env, ctx, prefix, rest);
  },
};
