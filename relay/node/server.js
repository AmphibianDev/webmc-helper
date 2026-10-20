// The WebMC relay, for a host that is not Cloudflare.
//
// relay.js in the parent directory is a Cloudflare Worker, which is the cheapest way to run this and
// the only one that needs no server at all. But a Worker can only live on a hostname Cloudflare gives
// you — a workers.dev subdomain or a domain you own — and a school network that blocks workers.dev
// wholesale takes the relay with it. This file is the same relay, written for any host that runs Node
// (Render, Heroku, Fly, Replit, Railway, a VPS, or your own laptop), so that it can live on whatever
// hostname your network happens to allow.
//
// Same wire protocol as the Worker, so the helper cannot tell them apart:
//
//   GET  /<prefix>/<path>   fetch, and stream back, from a known upstream (Mojang, Fabric, Modrinth…)
//   GET  /health            what this deployment offers
//   POST /v1/token          a signed, short-lived token for one host and one port
//   GET  /v1/connect        a WebSocket, subprotocol goldproxy.v1, carrying raw TCP
//
// No dependencies: Node's own http, net and crypto are the whole stack, so deploy is "run node
// server.js" with no install step and nothing to keep patched.
//
//   PORT=8790 node server.js
//
// Environment:
//   PORT=8790                  (Render and friends set this for you)
//   RELAY_SECRET=…             signing key for tokens; set it on anything public
//   RELAY_ALLOWED_PORTS=…      ports a session may dial (default: the usual Minecraft ones)
//   RELAY_ALLOW_PRIVATE=1      allow loopback/LAN targets (default: refuse)
//   RELAY_ALLOW_ANY_PORT=1     stop enforcing the port allowlist (default: enforce)
//   RELAY_MAX_SECONDS=1800     longest a single session may live

"use strict";

const http = require("http");
const net = require("net");
const crypto = require("crypto");
const { URL } = require("url");

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

const DEFAULT_PORTS = [25565, 25566, 25567, 25568, 25575, 25585, 19132];
const WINDOW_BYTES = 512 * 1024;
const CHUNK = 32 * 1024;
const MAX_BYTES = 512 * 1024 * 1024;
const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, HEAD, POST, OPTIONS",
  "access-control-allow-headers": "*",
};

// ------------------------------------------------------------------ policy

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

function allowedPorts() {
  if (process.env.RELAY_ALLOW_ANY_PORT === "1") return null;
  const fromEnv = (process.env.RELAY_ALLOWED_PORTS || "").split(",").map((s) => Number(s.trim())).filter(Boolean);
  return new Set(fromEnv.length ? fromEnv : DEFAULT_PORTS);
}

function portAllowed(port) {
  const ports = allowedPorts();
  return ports === null || ports.has(Number(port));
}

function privateAllowed() {
  return process.env.RELAY_ALLOW_PRIVATE === "1";
}

// ------------------------------------------------------------------ tokens

function b64url(buf) {
  return Buffer.from(buf).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function makeToken(host, port, ttlSeconds = 120) {
  const payload = b64url(JSON.stringify({
    host, port, expiresAt: Math.floor(Date.now() / 1000) + ttlSeconds,
  }));
  const sig = crypto.createHmac("sha256", process.env.RELAY_SECRET || "webmc-relay-development-secret")
    .update(payload).digest();
  return { token: payload + "." + b64url(sig), expiresAt: JSON.parse(Buffer.from(payload, "base64").toString()).expiresAt };
}

function readToken(token) {
  const [payload, sig] = String(token || "").split(".");
  if (!payload || !sig) return null;
  const expected = crypto.createHmac("sha256", process.env.RELAY_SECRET || "webmc-relay-development-secret")
    .update(payload).digest();
  if (!crypto.timingSafeEqual(Buffer.from(b64url(expected)), Buffer.from(sig))) return null;
  try {
    const data = JSON.parse(Buffer.from(payload.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString());
    if (!data.expiresAt || data.expiresAt < Math.floor(Date.now() / 1000)) return null;
    return data;
  } catch { return null; }
}

// ------------------------------------------------------------------ the handshake guard

// A relay that dialled any port for any bytes would be a port scanner with a nicer interface, so a
// session has to start the way a Minecraft client starts: a VarInt length, then packet id 0. The
// first bytes can arrive split, so wait for up to eight of them, which is always enough.
function looksLikeMinecraftHandshake(bytes) {
  if (!bytes || bytes.length < 2) return false;
  let i = 0, length = 0, shift = 0;
  while (i < bytes.length && i < 5) {
    const b = bytes[i++];
    length |= (b & 0x7f) << shift;
    shift += 7;
    if (!(b & 0x80)) break;
  }
  if (length < 2 || length > 4096) return false;
  if (i >= bytes.length) return false;
  return bytes[i] === 0x00;
}

// ------------------------------------------------------------------ WebSocket, by hand, no deps

function acceptKey(key) {
  return crypto.createHash("sha1").update(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64");
}

function encodeFrame(opcode, payload) {
  const len = payload.length;
  let header;
  if (len < 126) {
    header = Buffer.alloc(2); header[1] = len;
  } else if (len < 65536) {
    header = Buffer.alloc(4); header[1] = 126; header.writeUInt16BE(len, 2);
  } else {
    header = Buffer.alloc(10); header[1] = 127; header.writeBigUInt64BE(BigInt(len), 2);
  }
  header[0] = 0x80 | opcode;
  return Buffer.concat([header, payload]);
}

class Frames {
  constructor(onMessage, onClose) {
    this.buffer = Buffer.alloc(0);
    this.onMessage = onMessage;
    this.onClose = onClose;
    this.fragment = [];
    this.fragmentOpcode = 0;
  }

  push(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      if (this.buffer.length < 2) return;
      const first = this.buffer[0], second = this.buffer[1];
      const masked = (second & 0x80) !== 0;
      let len = second & 0x7f, offset = 2;
      if (len === 126) { if (this.buffer.length < 4) return; len = this.buffer.readUInt16BE(2); offset = 4; }
      else if (len === 127) { if (this.buffer.length < 10) return; len = Number(this.buffer.readBigUInt64BE(2)); offset = 10; }
      let mask = null;
      if (masked) { if (this.buffer.length < offset + 4) return; mask = this.buffer.subarray(offset, offset + 4); offset += 4; }
      if (this.buffer.length < offset + len) return;
      let payload = Buffer.from(this.buffer.subarray(offset, offset + len));
      if (mask) for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i % 4];
      this.buffer = this.buffer.subarray(offset + len);

      const opcode = first & 0x0f;
      const fin = (first & 0x80) !== 0;
      if (opcode === 0x8) { this.onClose(); return; }
      if (opcode === 0x9) { this.onMessage({ control: "ping", payload }); continue; }
      if (opcode === 0xa) { this.onMessage({ control: "pong", payload }); continue; }
      if (opcode === 0x0) {
        this.fragment.push(payload);
      } else {
        this.fragment = [payload];
        this.fragmentOpcode = opcode;
      }
      if (!fin) continue;
      const whole = Buffer.concat(this.fragment);
      this.fragment = [];
      this.onMessage({ opcode: this.fragmentOpcode, payload: whole });
    }
  }
}

// ------------------------------------------------------------------ helpers

function json(res, status, body) {
  const payload = Buffer.from(JSON.stringify(body, null, 1));
  res.writeHead(status, { "content-type": "application/json", ...CORS });
  res.end(payload);
}

function readBody(req, limit = 4096) {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (chunk) => { data += chunk; if (data.length > limit) req.destroy(); });
    req.on("end", () => { try { resolve(JSON.parse(data || "{}")); } catch { resolve({}); } });
    req.on("error", () => resolve({}));
  });
}

// ------------------------------------------------------------------ the tunnel

function serveTunnel(req, socket, head) {
  const key = req.headers["sec-websocket-key"];
  const protocols = String(req.headers["sec-websocket-protocol"] || "").split(",").map((s) => s.trim());
  if (!key || !protocols.includes("goldproxy.v1")) {
    socket.write("HTTP/1.1 400 Bad Request\r\n\r\n");
    socket.destroy();
    return;
  }
  socket.write("HTTP/1.1 101 Switching Protocols\r\n" +
               "Upgrade: websocket\r\nConnection: Upgrade\r\n" +
               "Sec-WebSocket-Accept: " + acceptKey(key) + "\r\n" +
               "Sec-WebSocket-Protocol: goldproxy.v1\r\n\r\n");
  socket.setNoDelay(true);

  let target = null, finished = false, written = 0;
  let checkedHandshake = false, heardAnything = false;
  let handshakeBuffer = Buffer.alloc(0);
  let idle = null, silent = null, deadline = null;

  const send = (opcode, payload) => { if (!finished) { try { socket.write(encodeFrame(opcode, payload)); } catch {} } };
  const sendText = (obj) => send(0x1, Buffer.from(JSON.stringify(obj)));
  const sendBinary = (buf) => send(0x2, buf);

  function finish(code, message) {
    if (finished) return;
    finished = true;
    clearTimeout(deadline); clearTimeout(silent); clearTimeout(idle);
    sendText({ t: "end", code, message });
    try { if (target) target.destroy(); } catch {}
    try { socket.end(); } catch {}
  }

  function checkHandshake(chunk) {
    handshakeBuffer = Buffer.concat([handshakeBuffer, chunk]);
    if (looksLikeMinecraftHandshake(handshakeBuffer)) { checkedHandshake = true; return true; }
    if (handshakeBuffer.length >= 8) {
      finish("NOT_MINECRAFT", "a session has to begin with a Minecraft handshake");
      return false;
    }
    return null;                       // not enough bytes yet: wait
  }

  const maxSeconds = Number(process.env.RELAY_MAX_SECONDS || 1800);
  deadline = setTimeout(() => finish("IDLE", "the session reached its time limit"), maxSeconds * 1000);

  const frames = new Frames((message) => {
    if (finished) return;
    if (message.control === "ping") { send(0xa, message.payload); return; }
    if (message.control === "pong") return;
    const bytes = message.payload;
    if (message.opcode === 0x1) {
      let body = {};
      try { body = JSON.parse(bytes.toString("utf8")); } catch { return; }
      if (body.t === "open") {
        const host = String(body.host || "");
        const port = Number(body.port);
        const token = readToken(body.token);
        if (!token || token.host !== host || Number(token.port) !== port) {
          sendText({ t: "error", code: "UNAUTHORIZED", message: "that token is not for this address" });
          finish("REFUSED", "bad token"); return;
        }
        if (!portAllowed(port)) {
          sendText({ t: "error", code: "PORT_BLOCKED", message: `port ${port} is not one this relay dials` });
          finish("REFUSED", "port not allowed"); return;
        }
        if (isPrivateHost(host) && !privateAllowed()) {
          sendText({ t: "error", code: "ADDRESS_BLOCKED", message: "that address is private or reserved" });
          finish("REFUSED", "private address"); return;
        }
        const started = Date.now();
        target = net.connect({ host, port });
        target.setNoDelay(true);
        target.on("connect", () => {
          sendText({ t: "ready", ip: host, port, connectMs: Date.now() - started, window: WINDOW_BYTES });
          silent = setTimeout(() => {
            if (!heardAnything) {
              sendText({ t: "error", code: "NO_DATA", message: "the server accepted the connection but sent nothing" });
              finish("IDLE", "no data from the target");
            }
          }, 20000);
        });
        target.on("data", (chunk) => {
          heardAnything = true;
          for (let off = 0; off < chunk.length; off += CHUNK) sendBinary(chunk.subarray(off, off + CHUNK));
        });
        target.on("error", (err) => {
          sendText({ t: "error", code: "CONNECT_FAILED", message: String(err && err.message || err) });
          finish("END", "the target refused the connection");
        });
        target.on("close", () => finish("END", "the server closed the connection"));
      } else if (body.t === "ping") {
        sendText({ t: "pong", id: body.id });
      }
      return;
    }
    if (message.opcode === 0x2 && target && target.writable) {
      const verdict = checkedHandshake ? true : checkHandshake(bytes);
      if (verdict === false) return;
      written += bytes.length;
      if (written > MAX_BYTES) { finish("MESSAGE_TOO_LARGE", "this session has moved too much data"); return; }
      target.write(bytes);
    }
  }, () => finish("END", "closed"));

  socket.on("data", (chunk) => frames.push(chunk));
  socket.on("close", () => finish("END", "closed"));
  socket.on("error", () => finish("END", "socket error"));
  if (head && head.length) frames.push(head);
}

// ------------------------------------------------------------------ files

async function serveFile(req, res, prefix, rest, search) {
  const base = UPSTREAM[prefix];
  if (!base) {
    return json(res, 404, { error: "unknown prefix", prefix, known: Object.keys(UPSTREAM) });
  }
  const target = base + "/" + rest.join("/") + search;
  try {
    const upstream = await fetch(target, {
      method: req.method === "HEAD" ? "GET" : req.method,
      headers: { "user-agent": req.headers["user-agent"] || "webmc-relay" },
    });
    if (!upstream.ok && upstream.status >= 500) {
      return json(res, 502, { error: "UPSTREAM_ERROR", upstream: base, status: upstream.status });
    }
    const headers = { ...CORS, "content-type": upstream.headers.get("content-type") || "application/octet-stream" };
    const length = upstream.headers.get("content-length");
    if (length) headers["content-length"] = length;
    headers["cache-control"] = "public, max-age=3600";
    headers["x-relayed-from"] = base;
    res.writeHead(upstream.status, headers);
    if (req.method === "HEAD" || !upstream.body) return res.end();
    const reader = upstream.body.getReader();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      if (!res.write(Buffer.from(value))) await new Promise((r) => res.once("drain", r));
    }
    res.end();
  } catch (err) {
    return json(res, 502, { error: "UPSTREAM_UNREACHABLE", upstream: base, path: rest.join("/"),
                            message: String(err && err.message || err) });
  }
}

// ------------------------------------------------------------------ entry

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  if (req.method === "OPTIONS") { res.writeHead(204, CORS); return res.end(); }

  if (url.pathname === "/" || url.pathname === "/health") {
    const ports = allowedPorts();
    return json(res, 200, { ok: true, service: "webmc-relay-node", files: Object.keys(UPSTREAM),
                            tunnel: true, protocol: "goldproxy.v1",
                            ports: ports === null ? "any (RELAY_ALLOW_ANY_PORT=1)" : [...ports],
                            privateTargets: privateAllowed() });
  }

  if (url.pathname === "/v1/token") {
    if (req.method !== "POST") return json(res, 405, { error: "POST {host,port}" });
    const body = await readBody(req);
    const host = String(body.host || "").trim();
    const port = Number(body.port);
    if (!host || !port) return json(res, 400, { error: "host and port are required" });
    if (isPrivateHost(host) && !privateAllowed()) {
      return json(res, 403, { error: "ADDRESS_BLOCKED", message: "that address is in a private or reserved network" });
    }
    if (!portAllowed(port)) {
      return json(res, 403, { error: "PORT_BLOCKED", message: `port ${port} is not one this relay dials` });
    }
    return json(res, 200, makeToken(host, port));
  }

  const parts = url.pathname.split("/").filter(Boolean);
  return serveFile(req, res, parts[0], parts.slice(1), url.search);
});

server.on("upgrade", (req, socket, head) => {
  const url = new URL(req.url, "http://localhost");
  if (url.pathname !== "/v1/connect") { socket.destroy(); return; }
  serveTunnel(req, socket, head);
});

const port = Number(process.env.PORT || 8790);
server.listen(port, () => {
  console.log("webmc-relay-node listening on " + port);
  console.log("  files:  " + Object.keys(UPSTREAM).join(", "));
  console.log("  tunnel: goldproxy.v1, ports " + JSON.stringify(allowedPorts() === null ? "any" : [...allowedPorts()]));
  console.log("  secret: " + (process.env.RELAY_SECRET ? "set" : "NOT SET (tokens are forgeable; set RELAY_SECRET)"));
});
