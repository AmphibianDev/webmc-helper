#!/usr/bin/env python3
"""Check a deployed WebMC relay, and say plainly what it does and does not do.

    python3 relay/test-relay.py https://webmc-relay.you.workers.dev

Everything here is run from this machine, over ordinary HTTPS, so it answers the question that
matters: from a network that blocks Mojang, can the relay still hand me the game? It uses the same
Tunnel and WebSocket code the launcher uses, so a pass here is a pass there.
"""

import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "helper"))

PASS, FAIL, INFO = "PASS", "FAIL", "    "
results = []


def say(state, what, detail=""):
    results.append(state == PASS)
    line = "  %-4s %s" % (state, what)
    if detail:
        line += "  — " + str(detail)
    print(line)


def http(url, data=None, method=None, timeout=25):
    request = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "User-Agent": "webmc-relay-test"},
        method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(200000), dict(response.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read(200000), dict(err.headers)


def varint(value):
    out = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        out += bytes([byte | (0x80 if value else 0)])
        if not value:
            return out


def handshake(host, port, state=1, protocol=767):
    body = (varint(0) + varint(protocol) + varint(len(host)) + host.encode()
            + struct.pack(">H", port) + varint(state))
    return varint(len(body)) + body


def status_of(relay, host, port=25565, seconds=12):
    """Ask a real Minecraft server for its status, through the relay."""
    import webmc_helper as helper
    seen = []
    tunnel = helper.Tunnel(relay, host, port, log=seen.append)
    local = tunnel.start()
    try:
        conn = socket.create_connection(("127.0.0.1", local), timeout=seconds)
        conn.sendall(handshake(host, port) + varint(1) + varint(0))   # status request
        conn.settimeout(seconds)
        blob = b""
        deadline = time.time() + seconds
        while time.time() < deadline and b"description" not in blob and b'"version"' not in blob:
            chunk = conn.recv(65536)
            if not chunk:
                break
            blob += chunk
        conn.close()
        return blob, seen
    finally:
        tunnel.stop()


def main():
    relay = (sys.argv[1] if len(sys.argv) > 1 else "").rstrip("/")
    if not relay.startswith("http"):
        print(__doc__)
        return 2
    print("Checking %s\n" % relay)

    try:
        status, body, _ = http(relay + "/health")
    except Exception as err:
        print("  FAIL  the relay could not be reached at all  — %s" % str(err)[:90])
        print("\n  Is it deployed, and is the URL exact? (no trailing slash, https://…)")
        return 1
    health = {}
    try:
        health = json.loads(body)
    except Exception:
        pass
    say(PASS if status == 200 and health.get("ok") else FAIL, "the relay answers at /health",
        "..".join(health.get("files", []))[:70])
    if not health.get("ok"):
        print("\nThat is not a WebMC relay, or it is not deployed yet.")
        return 1
    say(PASS if health.get("tunnel") else FAIL, "it offers the TCP tunnel",
        "protocol %s, ports %s" % (health.get("protocol"), health.get("ports")))

    # The reason most people deploy this: Mojang is blocked, and the relay is not.
    status, body, headers = http(relay + "/piston-meta/mc/game/version_manifest_v2.json")
    manifest = {}
    try:
        manifest = json.loads(body)
    except Exception:
        pass
    ok = status == 200 and isinstance(manifest.get("versions"), list)
    say(PASS if ok else FAIL, "it fetches the Minecraft version list from Mojang",
        "%s, %d versions, via %s" % (status, len(manifest.get("versions", [])),
                                     headers.get("X-Relayed-From", "?")))
    if ok:
        newest = manifest.get("latest", {}).get("release")
        say(PASS, "and the newest release is", str(newest))

    status, body, _ = http(relay + "/v1/token", data={"host": "127.0.0.1", "port": 25565})
    say(PASS if status == 403 else FAIL, "it refuses a private address",
        json.loads(body).get("error", status) if body else status)

    status, body, _ = http(relay + "/v1/token", data={"host": "play.example.net", "port": 22})
    say(PASS if status == 403 else FAIL, "it refuses a port that is not Minecraft's",
        json.loads(body).get("error", status) if body else status)

    status, body, _ = http(relay + "/v1/token", data={"host": "mc.hypixel.net", "port": 25565})
    token = {}
    try:
        token = json.loads(body)
    except Exception:
        pass
    say(PASS if status == 200 and token.get("token") else FAIL, "it issues a signed token for a real server",
        "expires in %ds" % max(0, token.get("expiresAt", 0) - time.time()) if token else status)

    print("\n  Asking a real Minecraft server for its status, through the relay…")
    try:
        blob, seen = status_of(relay, "mc.hypixel.net")
        text = blob.decode("utf-8", "replace")
        if '"description"' in text or '"version"' in text:
            start = text.find("{")
            try:
                info = json.loads(text[start:text.rfind("}") + 1])
                say(PASS, "a real server answered through the relay",
                    "players %s/%s" % (info.get("players", {}).get("online"),
                                       info.get("players", {}).get("max")))
            except Exception:
                say(PASS, "a real server answered through the relay", text[:60])
        else:
            say(INFO, "the test server did not answer (it may block proxies; not a relay fault)",
                text[:60].replace("\n", " ") or "no bytes")
    except Exception as err:
        say(INFO, "the test server could not be reached through the relay", str(err)[:70])

    print()
    if all(results):
        print("  Everything this machine can check, passes.")
        print("\n  In the launcher: Settings → If a network blocks things → Relay address")
        print("    %s" % relay)
        print("  Press “Test the relay”, then PLAY. Downloads go through the relay from now on;")
        print("  put a server in “Join this server” and tick “Send the game through the relay” if")
        print("  game ports are blocked too.")
        return 0
    print("  Something failed — the first FAIL above is the one to fix.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
