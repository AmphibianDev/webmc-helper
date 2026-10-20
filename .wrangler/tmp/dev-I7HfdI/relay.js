var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// relay/relay.js
import { connect } from "cloudflare:sockets";
var UPSTREAM = {
  "piston-meta": "https://piston-meta.mojang.com",
  "piston-data": "https://piston-data.mojang.com",
  "resources": "https://resources.download.minecraft.net",
  "libraries": "https://libraries.minecraft.net",
  "fabric-maven": "https://maven.fabricmc.net",
  "fabric-meta": "https://meta.fabricmc.net",
  "forge-maven": "https://maven.minecraftforge.net",
  "neoforge-maven": "https://maven.neoforged.net/releases",
  "modrinth": "https://api.modrinth.com",
  "adoptium": "https://api.adoptium.net"
};
var CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
  "Access-Control-Allow-Headers": "*"
};
var DEFAULT_PORTS = [25565, 25566, 25567, 25568, 25575, 25585, 19132];
var WINDOW_BYTES = 512 * 1024;
var CHUNK = 32 * 1024;
var MAX_BYTES = 512 * 1024 * 1024;
function json(body, init = {}) {
  return new Response(
    JSON.stringify(body, null, 1),
    { ...init, headers: { "Content-Type": "application/json", ...CORS, ...init.headers || {} } }
  );
}
__name(json, "json");
function isPrivateHost(host) {
  const v4 = /^(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(host);
  if (v4) {
    const [a, b] = [Number(v4[1]), Number(v4[2])];
    return a === 0 || a === 10 || a === 127 || a === 169 && b === 254 || a === 172 && b >= 16 && b <= 31 || a === 192 && b === 168 || a === 100 && b >= 64 && b <= 127;
  }
  const lower = host.toLowerCase().replace(/^\[|\]$/g, "");
  if (lower === "localhost" || lower.endsWith(".local") || lower.endsWith(".internal")) return true;
  if (/^f[cd][0-9a-f]{2}:/.test(lower) || /^fe80:/.test(lower) || lower === "::1") return true;
  return false;
}
__name(isPrivateHost, "isPrivateHost");
function allowedPorts(env) {
  if (env.RELAY_ALLOW_ANY_PORT === "1") return null;
  const fromEnv = (env.RELAY_ALLOWED_PORTS || "").split(",").map((s) => Number(s.trim())).filter(Boolean);
  return new Set(fromEnv.length ? fromEnv : DEFAULT_PORTS);
}
__name(allowedPorts, "allowedPorts");
function portAllowed(env, port) {
  const ports = allowedPorts(env);
  return ports === null || ports.has(Number(port));
}
__name(portAllowed, "portAllowed");
var enc = new TextEncoder();
function b64url(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
__name(b64url, "b64url");
async function key(env) {
  const secret = env.RELAY_SECRET || "webmc-relay-development-secret";
  return crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"]
  );
}
__name(key, "key");
async function makeToken(env, host, port, ttlSeconds = 120) {
  const expiresAt = Math.floor(Date.now() / 1e3) + ttlSeconds;
  const payload = b64url(enc.encode(JSON.stringify({ host, port, expiresAt })));
  const sig = await crypto.subtle.sign("HMAC", await key(env), enc.encode(payload));
  return { token: payload + "." + b64url(sig), expiresAt };
}
__name(makeToken, "makeToken");
async function readToken(env, token) {
  const [payload, sig] = String(token || "").split(".");
  if (!payload || !sig) return null;
  const expected = await crypto.subtle.sign("HMAC", await key(env), enc.encode(payload));
  if (b64url(expected) !== sig) return null;
  try {
    const data = JSON.parse(new TextDecoder().decode(Uint8Array.from(
      atob(payload.replace(/-/g, "+").replace(/_/g, "/")),
      (c) => c.charCodeAt(0)
    )));
    if (!data.expiresAt || data.expiresAt < Math.floor(Date.now() / 1e3)) return null;
    return data;
  } catch {
    return null;
  }
}
__name(readToken, "readToken");
function concat(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}
__name(concat, "concat");
function looksLikeMinecraftHandshake(bytes) {
  if (!bytes || bytes.length < 3) return false;
  let i = 0, length = 0, shift = 0;
  while (i < bytes.length && i < 5) {
    const b = bytes[i++];
    length |= (b & 127) << shift;
    shift += 7;
    if (!(b & 128)) break;
  }
  if (length < 2 || length > 4096) return false;
  if (i >= bytes.length) return false;
  const id = bytes[i];
  return id === 0;
}
__name(looksLikeMinecraftHandshake, "looksLikeMinecraftHandshake");
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
__name(dial, "dial");
async function serveTunnel(request, env) {
  if (request.headers.get("Upgrade") !== "websocket") {
    return json({ error: "this endpoint wants a WebSocket upgrade" }, { status: 426 });
  }
  if (!(request.headers.get("Sec-WebSocket-Protocol") || "").split(",").map((s) => s.trim()).includes("goldproxy.v1")) {
    return new Response("expected the goldproxy.v1 subprotocol", { status: 400, headers: CORS });
  }
  const pair = new WebSocketPair();
  const [client, server] = Object.values(pair);
  server.accept();
  let socket = null, writer = null, finished = false, received = 0, written = 0;
  let checkedHandshake = false, heardAnything = false, silent = null;
  const limit = Number(env.RELAY_MAX_SECONDS || 1800) * 1e3;
  const deadline = setTimeout(() => finish("IDLE", "the session reached its time limit"), limit);
  let handshakeBuffer = new Uint8Array(0);
  async function checkHandshake(bytes) {
    handshakeBuffer = concat(handshakeBuffer, bytes);
    if (looksLikeMinecraftHandshake(handshakeBuffer)) {
      checkedHandshake = true;
      return;
    }
    if (handshakeBuffer.length >= 8) {
      finish("NOT_MINECRAFT", "a session has to begin with a Minecraft handshake");
    }
  }
  __name(checkHandshake, "checkHandshake");
  function finish(code, message) {
    if (finished) return;
    finished = true;
    clearTimeout(deadline);
    clearTimeout(silent);
    try {
      server.send(JSON.stringify({ t: "end", code, message }));
    } catch {
    }
    try {
      writer && writer.close();
    } catch {
    }
    try {
      socket && socket.close();
    } catch {
    }
    try {
      server.close();
    } catch {
    }
  }
  __name(finish, "finish");
  async function toSocket(bytes) {
    if (!checkedHandshake) {
      await checkHandshake(bytes);
      if (finished) return;
    }
    writer = writer || socket.writable.getWriter();
    written += bytes.length;
    if (written > MAX_BYTES) {
      finish("MESSAGE_TOO_LARGE", "this session has moved too much data");
      return;
    }
    await writer.write(bytes);
  }
  __name(toSocket, "toSocket");
  server.addEventListener("message", async (event) => {
    if (finished) return;
    if (typeof event.data === "string") {
      let message = {};
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message.t === "open") {
        const result = await dial(env, {
          host: message.host,
          port: message.port,
          token: message.token
        });
        if (result.error) {
          try {
            server.send(JSON.stringify(result.error));
          } catch {
          }
          finish("REFUSED", result.error.message);
          return;
        }
        socket = result.socket;
        server.send(JSON.stringify({
          t: "ready",
          ip: message.host,
          port: Number(message.port),
          connectMs: result.connectMs,
          window: WINDOW_BYTES
        }));
        silent = setTimeout(() => {
          if (!heardAnything) {
            try {
              server.send(JSON.stringify({
                t: "error",
                code: "NO_DATA",
                message: "the server accepted the connection but sent nothing"
              }));
            } catch {
            }
            finish("IDLE", "no data from the target");
          }
        }, 2e4);
        (async () => {
          const reader = socket.readable.getReader();
          try {
            for (; ; ) {
              const { value, done } = await reader.read();
              if (done) break;
              heardAnything = true;
              for (let off = 0; off < value.length; off += CHUNK) {
                server.send(value.subarray(off, off + CHUNK));
                received += Math.min(CHUNK, value.length - off);
              }
            }
          } catch (err) {
            try {
              server.send(JSON.stringify({ t: "error", code: "STREAM", message: String(err && err.message || err) }));
            } catch {
            }
          }
          finish("END", "the server closed the connection");
        })();
      } else if (message.t === "ack") {
        acked = Number(message.n) || 0;
      } else if (message.t === "ping") {
        try {
          server.send(JSON.stringify({ t: "pong", id: message.id }));
        } catch {
        }
      }
      return;
    }
    try {
      await toSocket(new Uint8Array(event.data));
    } catch (err) {
      finish("CONNECT_FAILED", String(err && err.message || err));
    }
  });
  server.addEventListener("close", () => finish("END", "closed"));
  return new Response(null, {
    status: 101,
    webSocket: client,
    headers: { "Sec-WebSocket-Protocol": "goldproxy.v1" }
  });
}
__name(serveTunnel, "serveTunnel");
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
    try {
      response = await fetch(target, {
        method: request.method === "HEAD" ? "GET" : request.method,
        headers: { "User-Agent": request.headers.get("User-Agent") || "webmc-relay" }
      });
    } catch (err) {
      return json({
        error: "UPSTREAM_UNREACHABLE",
        upstream: base,
        path: rest.join("/"),
        message: String(err && err.message || err)
      }, { status: 502 });
    }
    if (response.status >= 500) {
      return json(
        { error: "UPSTREAM_ERROR", upstream: base, status: response.status },
        { status: 502 }
      );
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
__name(serveFile, "serveFile");
var relay_default = {
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
        privateTargets: env.RELAY_ALLOW_PRIVATE === "1"
      });
    }
    if (url.pathname === "/v1/token") {
      if (request.method !== "POST") return json({ error: "POST {host,port}" }, { status: 405 });
      let body = {};
      try {
        body = await request.json();
      } catch {
        return json({ error: "expected JSON" }, { status: 400 });
      }
      const host = String(body.host || "").trim();
      const port = Number(body.port);
      if (!host || !port) return json({ error: "host and port are required" }, { status: 400 });
      if (isPrivateHost(host) && env.RELAY_ALLOW_PRIVATE !== "1") {
        return json(
          { error: "ADDRESS_BLOCKED", message: "that address is in a private or reserved network" },
          { status: 403 }
        );
      }
      if (!portAllowed(env, port)) {
        return json(
          { error: "PORT_BLOCKED", message: `port ${port} is not one this relay dials` },
          { status: 403 }
        );
      }
      const { token, expiresAt } = await makeToken(env, host, port);
      return json({ token, expiresAt });
    }
    if (url.pathname === "/v1/connect") return serveTunnel(request, env);
    const [, prefix, ...rest] = url.pathname.split("/");
    if (!prefix) return json({ error: "nothing here" }, { status: 404 });
    return serveFile(request, env, ctx, prefix, rest);
  }
};

// ../../../.npm/_npx/d77349f55c2be1c0/node_modules/wrangler/templates/middleware/middleware-ensure-req-body-drained.ts
var drainBody = /* @__PURE__ */ __name(async (request, env, _ctx, middlewareCtx) => {
  try {
    return await middlewareCtx.next(request, env);
  } finally {
    try {
      if (request.body !== null && !request.bodyUsed) {
        const reader = request.body.getReader();
        while (!(await reader.read()).done) {
        }
      }
    } catch (e) {
      console.error("Failed to drain the unused request body.", e);
    }
  }
}, "drainBody");
var middleware_ensure_req_body_drained_default = drainBody;

// .wrangler/tmp/bundle-sWyF5F/middleware-insertion-facade.js
var __INTERNAL_WRANGLER_MIDDLEWARE__ = [
  middleware_ensure_req_body_drained_default
];
var middleware_insertion_facade_default = relay_default;

// ../../../.npm/_npx/d77349f55c2be1c0/node_modules/wrangler/templates/middleware/common.ts
var __facade_middleware__ = [];
function __facade_register__(...args) {
  __facade_middleware__.push(...args.flat());
}
__name(__facade_register__, "__facade_register__");
function __facade_invokeChain__(request, env, ctx, dispatch, middlewareChain) {
  const [head, ...tail] = middlewareChain;
  const middlewareCtx = {
    dispatch,
    next(newRequest, newEnv) {
      return __facade_invokeChain__(newRequest, newEnv, ctx, dispatch, tail);
    }
  };
  return head(request, env, ctx, middlewareCtx);
}
__name(__facade_invokeChain__, "__facade_invokeChain__");
function __facade_invoke__(request, env, ctx, dispatch, finalMiddleware) {
  return __facade_invokeChain__(request, env, ctx, dispatch, [
    ...__facade_middleware__,
    finalMiddleware
  ]);
}
__name(__facade_invoke__, "__facade_invoke__");

// .wrangler/tmp/bundle-sWyF5F/middleware-loader.entry.ts
var __Facade_ScheduledController__ = class ___Facade_ScheduledController__ {
  constructor(scheduledTime, cron, noRetry) {
    this.scheduledTime = scheduledTime;
    this.cron = cron;
    this.#noRetry = noRetry;
  }
  scheduledTime;
  cron;
  static {
    __name(this, "__Facade_ScheduledController__");
  }
  #noRetry;
  noRetry() {
    if (!(this instanceof ___Facade_ScheduledController__)) {
      throw new TypeError("Illegal invocation");
    }
    this.#noRetry();
  }
};
function wrapExportedHandler(worker) {
  if (__INTERNAL_WRANGLER_MIDDLEWARE__ === void 0 || __INTERNAL_WRANGLER_MIDDLEWARE__.length === 0) {
    return worker;
  }
  for (const middleware of __INTERNAL_WRANGLER_MIDDLEWARE__) {
    __facade_register__(middleware);
  }
  const fetchDispatcher = /* @__PURE__ */ __name(function(request, env, ctx) {
    if (worker.fetch === void 0) {
      throw new Error("Handler does not export a fetch() function.");
    }
    return worker.fetch(request, env, ctx);
  }, "fetchDispatcher");
  return {
    ...worker,
    fetch(request, env, ctx) {
      const dispatcher = /* @__PURE__ */ __name(function(type, init) {
        if (type === "scheduled" && worker.scheduled !== void 0) {
          const controller = new __Facade_ScheduledController__(
            Date.now(),
            init.cron ?? "",
            () => {
            }
          );
          return worker.scheduled(controller, env, ctx);
        }
      }, "dispatcher");
      return __facade_invoke__(request, env, ctx, dispatcher, fetchDispatcher);
    }
  };
}
__name(wrapExportedHandler, "wrapExportedHandler");
function wrapWorkerEntrypoint(klass) {
  if (__INTERNAL_WRANGLER_MIDDLEWARE__ === void 0 || __INTERNAL_WRANGLER_MIDDLEWARE__.length === 0) {
    return klass;
  }
  for (const middleware of __INTERNAL_WRANGLER_MIDDLEWARE__) {
    __facade_register__(middleware);
  }
  return class extends klass {
    #fetchDispatcher = /* @__PURE__ */ __name((request, env, ctx) => {
      this.env = env;
      this.ctx = ctx;
      if (super.fetch === void 0) {
        throw new Error("Entrypoint class does not define a fetch() function.");
      }
      return super.fetch(request);
    }, "#fetchDispatcher");
    #dispatcher = /* @__PURE__ */ __name((type, init) => {
      if (type === "scheduled" && super.scheduled !== void 0) {
        const controller = new __Facade_ScheduledController__(
          Date.now(),
          init.cron ?? "",
          () => {
          }
        );
        return super.scheduled(controller);
      }
    }, "#dispatcher");
    fetch(request) {
      return __facade_invoke__(
        request,
        this.env,
        this.ctx,
        this.#dispatcher,
        this.#fetchDispatcher
      );
    }
  };
}
__name(wrapWorkerEntrypoint, "wrapWorkerEntrypoint");
var WRAPPED_ENTRY;
if (typeof middleware_insertion_facade_default === "object") {
  WRAPPED_ENTRY = wrapExportedHandler(middleware_insertion_facade_default);
} else if (typeof middleware_insertion_facade_default === "function") {
  WRAPPED_ENTRY = wrapWorkerEntrypoint(middleware_insertion_facade_default);
}
var middleware_loader_entry_default = WRAPPED_ENTRY;
export {
  __INTERNAL_WRANGLER_MIDDLEWARE__,
  middleware_loader_entry_default as default
};
//# sourceMappingURL=relay.js.map
