#!/usr/bin/env python3
"""
webmc-helper - run real Minecraft + Fabric on this machine, keep nothing.

The contract, in the words of the design it implements:

    the page only launches Minecraft; Minecraft still runs on your computer,
    just ... temporarily.

So every byte this helper writes goes into one sandbox directory under /tmp
(`/tmp/webmc-<nonce>`), and the whole thing is deleted when the session's files
have been handed to the page - or after `--autoclean` seconds, whichever is
first. Nothing is installed, no service is registered, nothing is left behind.

    page (github.io or this helper's own /)  ──HTTP──▶  127.0.0.1:7645
                                                            │
                                          downloads ────────┤ Mojang / Fabric / Modrinth
                                          (through the     │
                                           SOCKS tunnel    │
                                           when direct     │
                                           is blocked)     │
                                                            ▼
                                          /tmp/webmc-XXXX/  ──▶ java ... KnotClient
                                                            │
                                          saves + logs ─────┤ zip
                                                            ▼
                                                    page stores it (OPFS)
                                                            │
                                                    POST /api/cleanup ──▶ rm -rf

Endpoints (all JSON):
    GET  /                  the page, with this helper's own token baked in
    GET  /go?to=<page-url>  redirect to a hosted page with the token in the fragment    GET  /api/hello         identity + capability probe (no token needed)
    GET  /api/versions      Mojang's release list (cached, fetched lazily)
    GET  /api/search?q=&mc= Modrinth search, run here so it works behind a filtered network
    GET  /api/status        phase, progress, instance, pid, exit code, cleanup receipt
    GET  /api/log?since=N   log lines after index N
    GET  /api/localmods     the mod jars the page has handed over for this session
    POST /api/launch        {version, loader, mods, username, memory, assets}
    POST /api/stop          kill the game
    POST /api/localmods     body = one .jar, name in X-WebMC-Mod-Name (?clear=1, ?drop=name)
    POST /api/restore       body = a bundle zip from a previous session
    GET  /api/result        exit code, collected files, bundle size
    GET  /api/bundle.zip    the collected saves/logs/screenshot bundle
    POST /api/cleanup       delete the sandbox

Only Python 3.9's standard library is used. Downloads shell out to `curl`,
because curl speaks SOCKS5 with remote DNS (`socks5h://`) and does parallel
transfers, which is exactly what a poisoned resolver needs.
"""

import argparse
import base64
import errno
import hashlib
import http.server
import io
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid as uuidlib
import zipfile

VERSION = "0.1.0"
NAME = "webmc-helper"

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RESOURCES_URL = "https://resources.download.minecraft.net"
FABRIC_META = "https://meta.fabricmc.net/v2"
# Forge and NeoForge publish a version document *inside their installer jar*, which is what a
# launcher needs - no installation step, no patching by hand. Reading it out is what makes the
# other two loaders the same shape as Fabric here.
LOADER_MAVEN = {
    "forge": "https://maven.minecraftforge.net/",
    "neoforge": "https://maven.neoforged.net/releases/",
}
LOADER_METADATA = {
    "forge": "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml",
    "neoforge": "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml",
}
LOADER_KINDS = ("fabric", "forge", "neoforge", "none")

# When Mojang (or Fabric, or Modrinth) is unreachable from the visitor's network, everything can
# be fetched through one relay instead: it maps straight onto the upstream URLs, so nothing else in
# this file has to know. See relay/ for the Cloudflare Worker that does it.
RELAY_PREFIXES = (
    ("https://piston-meta.mojang.com", "piston-meta"),
    ("https://piston-data.mojang.com", "piston-data"),
    ("https://resources.download.minecraft.net", "resources"),
    ("https://libraries.minecraft.net", "libraries"),
    ("https://maven.fabricmc.net", "fabric-maven"),
    ("https://meta.fabricmc.net", "fabric-meta"),
    ("https://maven.minecraftforge.net", "forge-maven"),
    ("https://maven.neoforged.net/releases", "neoforge-maven"),
    ("https://api.modrinth.com", "modrinth"),
    ("https://api.adoptium.net", "adoptium"),
)


# The relays this helper will use when the page (or the command line) does not name one. Deploying a
# Worker once and putting its URL here is what turns "everybody deploys their own" into "it works".
# Order matters: the first that answers /health wins, so list a spare. An empty tuple means the
# helper simply downloads directly, which is what most networks want anyway.
DEFAULT_RELAYS = (
    # Relays this project deployed for everyone, on two different Cloudflare hostnames: one hostname
    # can always be blocked, and a second is what makes "nobody has to deploy one" hold for more than
    # one network. The helper probes them in order and uses the first that answers /health; if none
    # does (a network that blocks Cloudflare entirely) it downloads directly, or through the
    # visitor's own proxy when there is one. Add any URL here and it joins the list.
    "https://webmc-relay.webmc-helper.workers.dev",
    "https://webmc-relay-pages.pages.dev",
)

_RELAY_CHOICE = {"probed": False, "url": None, "via": None}
OFF = ("off", "none", "no", "direct", "-")


def relay_reachable(url, timeout=5):
    """Does this relay answer /health? Cheap, and it is the only thing that decides."""
    try:
        request = urllib.request.Request(url.rstrip("/") + "/health",
                                        headers={"User-Agent": "webmc-helper"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def dns_query(name, qtype, timeout=5):
    """One DNS question, answered by hand.

    Python's standard library can resolve A records and nothing else, and Minecraft servers are
    usually reached through an SRV record: play.example.net is a signpost, not an address. Asking the
    question directly is the only way to follow that signpost without adding a dependency.
    """
    import random
    labels = b"".join(bytes([len(part)]) + part.encode() for part in name.rstrip(".").split("."))
    packet = (struct.pack("!HHHHHH", random.randrange(65535), 0x0100, 1, 0, 0, 0)
              + labels + b"\x00" + struct.pack("!HH", qtype, 1))
    for server in dns_servers():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(timeout)
            sock.sendto(packet, (server, 53))
            data, _ = sock.recvfrom(4096)
        except Exception:
            continue
        finally:
            sock.close()
        return parse_dns_answers(data)
    return []


def dns_servers():
    """Whatever this machine would ask, in order."""
    servers = []
    try:
        with open("/etc/resolv.conf") as fh:
            for line in fh:
                if line.startswith("nameserver"):
                    servers.append(line.split()[1])
    except Exception:
        pass
    for fallback in ("1.1.1.1", "8.8.8.8"):
        if fallback not in servers:
            servers.append(fallback)
    return servers


def parse_dns_answers(data):
    """(name, port, priority, weight) for every SRV record in a reply, best first."""
    if len(data) < 12:
        return []
    _id, _flags, qd, an, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
    offset = 12

    def skip_name(at):
        while at < len(data):
            length = data[at]
            if length == 0:
                return at + 1
            if length & 0xC0:
                return at + 2
            at += 1 + length
        return at

    for _ in range(qd):
        offset = skip_name(offset) + 4
    answers = []
    for _ in range(an):
        if offset >= len(data):
            break
        offset = skip_name(offset)
        if offset + 10 > len(data):
            break
        rtype, _rclass, _ttl, length = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        body = data[offset:offset + length]
        offset += length
        if rtype != 33 or len(body) < 7:
            continue
        priority, weight, port = struct.unpack("!HHH", body[:6])
        target = []
        at = 6
        while at < len(body) and body[at]:
            size = body[at]
            target.append(body[at + 1:at + 1 + size].decode("utf-8", "replace"))
            at += 1 + size
        answers.append((".".join(target), port, priority, weight))
    answers.sort(key=lambda item: (item[2], -item[3]))
    return answers


def minecraft_srv(host, via_proxy=None, log=None):
    """Where a Minecraft server really lives, or None if there is no signpost.

    A filtered network poisons the resolver, so a local answer is not an answer: when the machine's
    own DNS says nothing, the same question goes to a public resolver over HTTPS — through the proxy
    or relay this session is already using, so it is asked over a connection the network cannot edit.
    """
    try:
        for name, port, _priority, _weight in dns_query("_minecraft._tcp." + host, 33):
            if name and not name.startswith("0.0.0.0"):
                return name, int(port)
    except Exception:
        pass
    for endpoint in ("https://cloudflare-dns.com/dns-query?name=_minecraft._tcp.%s&type=SRV" % host,
                     "https://dns.google/resolve?name=_minecraft._tcp.%s&type=SRV" % host):
        try:
            answer = http_json(endpoint, via_proxy=via_proxy, timeout=15)
        except Exception:
            continue
        for record in answer.get("Answer", []):
            if record.get("type") != 33:
                continue
            parts = str(record.get("data", "")).split()
            if len(parts) >= 4 and parts[3] not in (".", "", "0.0.0.0"):
                if log:
                    log("srv: %s is really %s:%s (a public resolver said so)" % (host, parts[3].rstrip("."), parts[2]))
                return parts[3].rstrip("."), int(parts[2])
    return None


def http_json(url, body=None, via_proxy=None, timeout=10):
    """GET a URL and parse JSON, through a SOCKS5 proxy when one is given.

    The relay has to be probed the same way it will be used: on a network that blocks the relay's
    hostname, the direct probe fails while the proxied one succeeds, and only the second answer is
    the truth.
    """
    import ssl
    parts = urllib.parse.urlsplit(url)
    secure = parts.scheme in ("https", "wss")
    host = parts.hostname
    port = parts.port or (443 if secure else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if via_proxy:
        sock = socks5_connect(via_proxy, host, port, timeout=timeout)
    else:
        sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        payload = json.dumps(body).encode() if body is not None else None
        head = ("%s %s HTTP/1.1\r\nHost: %s\r\nAccept: application/json\r\n"
                "User-Agent: %s/%s\r\nConnection: close\r\n"
                % ("POST" if payload is not None else "GET", path, parts.netloc, NAME, VERSION))
        if payload is not None:
            head += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(payload)
        sock.sendall((head + "\r\n").encode() + (payload or b""))
        chunks = b""
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks += data
            if len(chunks) > 2000000:
                break
    finally:
        try:
            sock.close()
        except Exception:
            pass
    head, _sep, body = chunks.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1]) if b" " in head.split(b"\r\n")[0] else 0
    if status != 200:
        raise OSError("HTTP %s from %s" % (status, url))
    if b"transfer-encoding: chunked" in head.lower():
        # Nobody sends a Content-Length any more, and a chunk header in the middle of JSON reads as
        # "Extra data: line 2" three layers up. Undo it here, once.
        out, rest = b"", body
        while True:
            line, _sep2, rest = rest.partition(b"\r\n")
            try:
                size = int(line.split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                break
            out += rest[:size]
            rest = rest[size + 2:]
        body = out
    return json.loads(body.decode("utf-8", "replace"))


def http_get_json(url, via_proxy=None, timeout=10):
    return http_json(url, via_proxy=via_proxy, timeout=timeout)


def relay_reachable_via(url, via_proxy, timeout=10):
    return bool(http_get_json(url.rstrip("/") + "/health", via_proxy=via_proxy, timeout=timeout).get("ok"))


def resolve_relay(requested, log=None, timeout=5, via_proxy=None):
    """Decide which relay to use. "off" means none, a URL means that one, and nothing means the
    built-in list — probed once per helper, so a launch never pays for it twice."""
    if requested and str(requested).strip().lower() in OFF:
        return None
    if requested and str(requested).strip():
        return str(requested).strip().rstrip("/")
    if _RELAY_CHOICE["probed"]:
        return _RELAY_CHOICE["url"]
    _RELAY_CHOICE["probed"] = True
    for candidate in DEFAULT_RELAYS:
        if relay_reachable(candidate, timeout=timeout):
            _RELAY_CHOICE["url"] = candidate.rstrip("/")
            if log:
                log("relay: using the built-in relay %s (say \"off\" to download directly)"
                    % _RELAY_CHOICE["url"])
            return _RELAY_CHOICE["url"]
    # The relay's own hostname can be blocked while a proxy on this machine works. Then the relay is
    # still the right answer: it resolves names out there, which is exactly what a poisoned DNS
    # cannot do here. Ask it through the proxy before giving up on it.
    for candidate in DEFAULT_RELAYS:
        if via_proxy:
            try:
                if relay_reachable_via(candidate, via_proxy, timeout=max(timeout * 2, 12)):
                    _RELAY_CHOICE["url"] = candidate.rstrip("/")
                    _RELAY_CHOICE["via"] = via_proxy
                    if log:
                        log("relay: %s is not reachable from here, but it answers through %s, so that "
                            "is what this session will use" % (candidate, via_proxy))
                    return _RELAY_CHOICE["url"]
            except Exception:
                pass
    if DEFAULT_RELAYS and log:
        log("relay: none of the %d built-in relays answered, directly or through a proxy; "
            "downloading directly" % len(DEFAULT_RELAYS))
    return None


def relayed(url, relay):
    """Rewrite an upstream URL to go through the relay, if one is configured."""
    if not relay:
        return url
    base = relay.rstrip("/")
    for upstream, prefix in RELAY_PREFIXES:
        if url.startswith(upstream):
            return "%s/%s%s" % (base, prefix, url[len(upstream):])
    return url
# What this helper will actually launch. Forge and NeoForge are resolved, downloaded and merged
# below - that part works and was verified as far as their bootstrap - but neither ships a usable
# client jar: their own installer runs installertools over the vanilla jar to produce
# client-<mc>-<stamp>-srg.jar and ...-extra.jar, and the bootstrap refuses to start without them.
# Until that patch step is implemented, they are refused up front rather than half-launched.
SUPPORTED_LOADERS = ("fabric", "none")
MODRINTH_API = "https://api.modrinth.com/v2"
ADOPTIUM_API = "https://api.adoptium.net/v3"

DEFAULT_VERSION = "1.21.11"

# The single-file build (tools/publish-helper.py) puts the launcher page in here as base64, so
# one downloadable .command can serve its own page without a folder of files next to it. In the
# repository this is empty and the page is read from page/index.html as usual.
_EMBEDDED_PAGE = ""
DEFAULT_MEMORY = "2G"

# Origin allowlist for the browser-side fetch. The page is either served by this
# helper, or hosted on GitHub Pages, or served by the repo's own test server.
ORIGIN_PATTERNS = [
    re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"),
    re.compile(r"^https://localhost(:\d+)?$"),
    re.compile(r"^https://[a-z0-9-]+\.github\.io$"),
    re.compile(r"^https://[a-z0-9-]+\.pages\.dev$"),
]

PROXY_CANDIDATES = [
    "socks5h://127.0.0.1:1080",
    "socks5h://127.0.0.1:1088",
    "socks5h://127.0.0.1:10808",
    "socks5h://127.0.0.1:7890",
]

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def log_ts():
    return time.strftime("%H:%M:%S")


# --------------------------------------------------------------------------- os facts


def os_name():
    p = sys.platform
    if p == "darwin":
        return "osx"
    if p.startswith("win"):
        return "windows"
    return "linux"


def os_arch():
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return m


def os_version():
    if os_name() == "osx":
        return platform.mac_ver()[0]
    return platform.release()


# --------------------------------------------------------------------------- rules

def _rule_applies(rule, features):
    """Mojang's rule predicate: os name/arch/version plus launcher features."""
    cond = rule.get("os")
    if cond:
        if "name" in cond and cond["name"] != os_name():
            return False
        if "arch" in cond:
            want = cond["arch"]
            have = os_arch()
            # Mojang says "x86" for 32-bit intel; treat it as a prefix match so a
            # rule written for x86 also matches x86_64 only when it says so.
            if want == "x86":
                if have not in ("x86", "i386", "i686"):
                    return False
            elif want != have:
                return False
        if "version" in cond:
            try:
                if not re.search(cond["version"], os_version()):
                    return False
            except re.error:
                return False
    feats = rule.get("features")
    if feats:
        for k, v in feats.items():
            if bool(features.get(k, False)) != bool(v):
                return False
    return True


def rules_allow(rules, features):
    """Empty rules mean allowed; otherwise the *last* matching rule decides."""
    if not rules:
        return True
    allow = False
    for rule in rules:
        if _rule_applies(rule, features):
            allow = (rule.get("action") == "allow")
    return allow


# --------------------------------------------------------------------------- curl

class CurlError(RuntimeError):
    pass


class Cancelled(Exception):
    """Raised when the visitor stops a launch that has not finished starting."""


class Curl:
    """Thin curl wrapper: SOCKS-aware, parallel-capable, sha1-verifying."""

    def __init__(self, proxy=None, retries=3):
        self.proxy = proxy
        self.retries = retries
        self.curl = shutil.which("curl") or "/usr/bin/curl"
        self._lock = threading.Lock()
        self.bytes_in = 0
        # Where temporary files (curl's --parallel config) are written. The session
        # points this at its own sandbox so that nothing of ours can survive outside
        # it, even if the helper dies mid-download.
        self.workdir = None
        self.relay = None
        # Running curl processes, so a cancel can kill a download in flight instead of making
        # somebody wait for 430 MB of assets when they picked the wrong version.
        self._live = set()
        self._abort = False

    def abort(self):
        """Stop every download now. Idempotent, safe to call from another thread."""
        with self._lock:
            self._abort = True
            live = list(self._live)
        for proc in live:
            try:
                proc.kill()
            except Exception:
                pass
        return len(live)

    def clear_abort(self):
        with self._lock:
            self._abort = False

    def aborted(self):
        with self._lock:
            return self._abort

    def _run_curl(self, args, timeout=None):
        """Every curl goes through here, tracked, so abort() can reach it."""
        if self.aborted():
            raise CurlError("cancelled")
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self._lock:
            self._live.add(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        finally:
            with self._lock:
                self._live.discard(proc)
        if self.aborted():
            raise CurlError("cancelled")
        return proc.returncode, out or b"", err or b""

    def workdir_path(self):
        d = self.workdir if (self.workdir and os.path.isdir(self.workdir)) \
            else os.environ.get("TMPDIR", "/tmp")
        return os.path.join(d, "webmc-curl-%s.conf" % secrets.token_hex(6))

    def _base(self):
        args = [self.curl, "-sS", "-L", "--fail", "--connect-timeout", "20",
                "--retry", str(self.retries), "--retry-connrefused",
                "--retry-delay", "1", "--create-dirs"]
        if self.proxy:
            args += ["--proxy", self.proxy]
        return args

    def get(self, url, dest=None, sha1=None, timeout=900, headers=()):
        """Download to dest (or return bytes when dest is None)."""
        url = relayed(url, getattr(self, "relay", None))
        if dest and sha1 and os.path.exists(dest) and sha1_of(dest) == sha1:
            return dest  # already there, and correct
        args = self._base() + ["-m", str(timeout)]
        for h in headers:
            args += ["-H", h]
        if dest:
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            tmp = dest + ".part"
            args += ["-o", tmp, url]
        else:
            args += [url]
        code, out, err = self._run_curl(args, timeout=timeout + 30)
        if code != 0:
            if dest and os.path.exists(dest + ".part"):
                os.unlink(dest + ".part")
            raise CurlError("curl %s -> %s: %s" % (
                url, code, err.decode("utf-8", "replace").strip()[:300]))
        if dest:
            if sha1 and sha1_of(tmp) != sha1:
                os.unlink(tmp)
                raise CurlError("sha1 mismatch for %s" % url)
            os.replace(tmp, dest)
            with self._lock:
                self.bytes_in += os.path.getsize(dest)
            return dest
        with self._lock:
            self.bytes_in += len(out)
        return out

    def json(self, url, timeout=120, headers=()):
        raw = self.get(url, dest=None, timeout=timeout, headers=headers)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise CurlError("bad json from %s: %s" % (url, exc))

    def batch(self, items, progress=None, workers=16, headers=()):
        """items: [(url, dest, sha1|None)]. Parallel, verifies, retries stragglers.

        Large batches go through a single `curl --parallel -K config` process:
        one process for thousands of objects, rather than thousands of processes.
        """
        todo = [it for it in items if not (it[2] and os.path.exists(it[1])
                                           and sha1_of(it[1]) == it[2])]
        if not todo:
            if progress:
                progress(len(items), len(items))
            return []
        if progress:
            progress(len(items) - len(todo), len(items))
        failed = []
        if len(todo) > 24:
            failed = self._batch_parallel(todo, progress, len(items),
                                          len(items) - len(todo), workers)
        else:
            failed = self._batch_threads(todo, progress, len(items),
                                         len(items) - len(todo), workers)
            failed = self._batch_parallel(todo, progress, len(items),
                                          len(items) - len(todo), workers)
        # verify + retry, individually, so a bad file can never survive
        for attempt in range(2):
            if not failed or self.aborted():
                break
            retry, failed = failed, []
            for item in retry:
                url, dest, sha1 = item
                try:
                    self.get(url, dest=dest, sha1=sha1, headers=headers)
                except CurlError as exc:
                    failed.append(item)
                    self._last_error = str(exc)
            if progress:
                progress(len(items) - len(failed), len(items), attempt + 2)
        return failed

    def _batch_threads(self, todo, progress, total, already, workers):
        from concurrent.futures import ThreadPoolExecutor
        failed = []
        done = [already]
        lock = threading.Lock()

        def one(item):
            url, dest, sha1 = item
            try:
                self.get(url, dest=dest, sha1=sha1)
            except CurlError:
                with lock:
                    failed.append(item)
            finally:
                with lock:
                    done[0] += 1
                    if progress:
                        progress(done[0], total)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, todo))
        return failed

    def _batch_parallel(self, todo, progress, total, already, workers):
        cfg = self.workdir_path()
        failed = []
        try:
            with open(cfg, "w") as fh:
                for url, dest, _sha1 in todo:
                    fh.write('url = "%s"\noutput = "%s"\n' % (url, dest))
            args = self._base() + ["--parallel", "--parallel-max", str(workers),
                                   "-K", cfg]
            if self.proxy:
                args += ["--proxy", self.proxy]
            proc = self._run_curl(args, timeout=1800)
            err = proc[2].decode("utf-8", "replace")
            code = proc[0]
            # which ones actually arrived intact?
            done = 0
            for item in todo:
                if os.path.exists(item[1]) and (not item[2] or sha1_of(item[1]) == item[2]):
                    done += 1
                else:
                    failed.append(item)
            if self.aborted():
                # A cancel is not a failure to report: nothing should be retried.
                raise CurlError("cancelled")
            if progress:
                progress(already + done, total)
            if failed and not err.strip():
                err = "(curl --parallel reported no detail)"
            self._last_error = err.strip()[-400:] if failed else ""
        finally:
            if os.path.exists(cfg):
                os.unlink(cfg)
        return failed


def sha1_of(path, chunk=1 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# --------------------------------------------------------------------------- java

VER_RE = re.compile(r'version "([^"]+)"')


def java_major(java_path):
    try:
        proc = subprocess.run([java_path, "-version"], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=30)
    except Exception:
        return 0
    out = proc.stdout.decode("utf-8", "replace")
    m = VER_RE.search(out)
    if not m:
        return 0
    v = m.group(1)
    if v.startswith("1."):
        try:
            return int(v.split(".")[1])
        except ValueError:
            return 0
    try:
        return int(re.match(r"(\d+)", v).group(1))
    except Exception:
        return 0


def find_javas():
    """Every java on the machine, best-effort, cheapest checks first."""
    cands = []
    env = os.environ.get("WEBMC_JAVA")
    if env:
        cands.append(env)
    home = os.environ.get("JAVA_HOME")
    if home:
        cands.append(os.path.join(home, "bin", "java"))
    for base, globs in (
        (os.path.expanduser("~/.jdks"), ("*/Contents/Home/bin/java", "*/bin/java")),
        ("/Library/Java/JavaVirtualMachines", ("*/Contents/Home/bin/java",)),
        (os.path.expanduser("~/Library/Java/JavaVirtualMachines"), ("*/Contents/Home/bin/java",)),
        ("/opt/homebrew/opt", ("openjdk*/bin/java", "openjdk@*/bin/java")),
        ("/usr/local/opt", ("openjdk*/bin/java", "openjdk@*/bin/java")),
    ):
        if not os.path.isdir(base):
            continue
        for g in globs:
            import glob as _glob
            cands.extend(sorted(_glob.glob(os.path.join(base, g)), reverse=True))
    which = shutil.which("java")
    if which:
        cands.append(which)
    out = []
    for c in cands:
        if c and os.path.exists(c) and os.access(c, os.X_OK) and c not in out:
            out.append(c)
    return out


def pick_java(required, explicit=None, log=None):
    if explicit:
        if not os.path.exists(explicit):
            raise RuntimeError("--java %s does not exist" % explicit)
        return explicit, java_major(explicit)
    found = []
    for c in find_javas():
        mj = java_major(c)
        if mj:
            found.append((mj, c))
    if not found:
        return None, 0
    found.sort(key=lambda t: -t[0])
    if log:
        log("java candidates: " + ", ".join("%d @ %s" % (m, c) for m, c in found[:6]))
    best_major, best = found[0]
    if best_major < required:
        return best, best_major  # caller decides whether to fetch a JRE
    return best, best_major


def fetch_jre(major, root, curl, log):
    arch = {"arm64": "aarch64", "x86_64": "x64"}.get(os_arch(), "x64")
    plat = "mac" if os_name() == "osx" else ("windows" if os_name() == "windows" else "linux")
    ext = "zip" if plat == "windows" else "tar.gz"
    url = "%s/binary/latest/%d/ga/%s/%s/jre/hotspot/normal/eclipse" % (
        ADOPTIUM_API, major, plat, arch)
    log("no Java %d here - fetching a JRE from Adoptium (%s/%s)" % (major, plat, arch))
    dest = os.path.join(root, "jre.%s" % ext)
    curl.get(url, dest=dest, timeout=1800)
    out = os.path.join(root, "jre")
    os.makedirs(out, exist_ok=True)
    if ext == "tar.gz":
        with tarfile.open(dest) as tf:
            tf.extractall(out)
    else:
        with zipfile.ZipFile(dest) as zf:
            zf.extractall(out)
    for base, _dirs, files in os.walk(out):
        if "java" in files and os.path.basename(base) == "bin":
            cand = os.path.join(base, "java")
            if os.access(cand, os.X_OK):
                return cand, java_major(cand)
    raise RuntimeError("fetched a JRE but could not find bin/java in it")


# --------------------------------------------------------------------------- downloads

def maven_to_path(coords):
    """group:artifact:version[:classifier] -> (relative path, file name)."""
    parts = coords.split(":")
    if len(parts) < 3:
        return None, None
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else None
    name = "%s-%s%s.jar" % (artifact, version, ("-" + classifier) if classifier else "")
    return "/".join([group.replace(".", "/"), artifact, version, name]), name


def vanilla_url(group, artifact, version, classifier=None, repo="https://libraries.minecraft.net"):
    rel, name = maven_to_path(":".join([group, artifact, version] + ([classifier] if classifier else [])))
    return "%s/%s" % (repo, rel), rel


class Registry:
    """Every session this helper has run, and the mod jars they share.

    A visitor may open two windows, or launch 1.21.11 next to 1.12.2, so "a session" is a thing the
    helper has many of rather than a property of the helper itself.
    """

    def __init__(self, cfg, curl, log=print):
        self.cfg = cfg
        self.curl = curl
        # Six places in here log through the relay (the SOCKS proxy opening a connection, the
        # tunnel coming up); without this they raised instead of logging.
        self.log = log
        self.lock = threading.RLock()
        self.sessions = {}
        self.order = []
        self.mods_dir = os.path.join(tempfile.gettempdir(), "webmc-mods-%d" % os.getpid())
        os.makedirs(self.mods_dir, exist_ok=True)
        self.pending_mods = []
        self.pending_restore = None
        self.started = time.time()
        self.last_touch = self.started
        self.store = session_store_dir(cfg)
        if getattr(cfg, "store_sessions", False):
            os.makedirs(self.store, exist_ok=True)

    def launch(self, opts):
        sid = secrets.token_hex(3)
        root = os.path.join("/tmp", "webmc-%s" % secrets.token_hex(4))
        cfg = argparse.Namespace(**vars(self.cfg))
        cfg.root = root
        requested = opts.get("relay")
        if requested is None:
            requested = getattr(self.cfg, "relay", None)
        cfg.relay = resolve_relay(requested, log=self.log)
        if opts.get("restricted"):
            cfg.restricted = True
        if opts.get("tunnelEverything"):
            cfg.proxy_game = True
        session = Session(cfg, self.curl, manager=self, sid=sid)
        session.pending_restore = self.pending_restore
        if session.pending_restore:
            session.pending_restore_from = "the page"
        self.pending_restore = None
        # Nothing from the page? Then start from what the helper kept last time, so a world does
        # not depend on which origin the visitor happened to press PLAY on.
        if session.pending_restore is None and opts.get("restore", True):
            kept = self.newest_stored(opts.get("version") or DEFAULT_VERSION)
            if kept:
                session.pending_restore, name = kept
                session.pending_restore_from = "the helper's own copy (%s)" % name
        with self.lock:
            self.sessions[sid] = session
            self.order.append(sid)
            while len(self.order) > 24:            # keep the page's list bounded
                oldest = self.order.pop(0)
                self.sessions.pop(oldest, None)
        session.start(opts)
        return session

    def touch(self):
        """The idle exit asks whether anybody is still using the helper at all, which is a
        question about the helper rather than about one of its sessions."""
        with self.lock:
            self.last_touch = time.time()

    # ---- keeping a session, and starting the next one from it
    def store_session(self, session):
        """Copy a finished session's bundle where the *next* launch can find it, whichever origin
        started this one."""
        if not getattr(self.cfg, "store_sessions", False) or not session.result:
            return None
        if not os.path.exists(session.bundle_path):
            return None
        version = (session.instance or {}).get("version") or "unknown"
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", "%s-%s" % (version, session.sid))
        dest = os.path.join(self.store, safe + ".zip")
        try:
            shutil.copy2(session.bundle_path, dest)
        except OSError as exc:
            session.log("could not keep the session for next time: %s" % exc)
            return None
        session.log("kept this session for next time: %s (%s)"
                    % (dest, ", ".join(session.result.get("worlds") or []) or "no world yet"))
        self.prune_store()
        return dest

    def prune_store(self):
        keep = max(1, int(getattr(self.cfg, "keep_sessions", 5) or 5))
        try:
            files = sorted((os.path.join(self.store, n) for n in os.listdir(self.store)
                            if n.endswith(".zip")), key=os.path.getmtime, reverse=True)
        except OSError:
            return
        for old in files[keep:]:
            try:
                os.unlink(old)
            except OSError:
                pass

    def newest_stored(self, version):
        """The most recent kept session for a version, for a launch that has nothing from the page."""
        if not getattr(self.cfg, "store_sessions", False):
            return None
        try:
            candidates = [os.path.join(self.store, n) for n in os.listdir(self.store)
                          if n.endswith(".zip") and n.startswith(re.sub(r"[^A-Za-z0-9._-]", "_", version) + "-")]
        except OSError:
            return None
        if not candidates:
            return None
        newest = max(candidates, key=os.path.getmtime)
        try:
            with open(newest, "rb") as fh:
                return fh.read(), os.path.basename(newest)
        except OSError:
            return None

    def pick(self, sid=None):
        with self.lock:
            if sid and sid in self.sessions:
                return self.sessions[sid]
            if not self.order:
                return None
            for candidate in reversed(self.order):
                session = self.sessions.get(candidate)
                if session and session.state in ("running", "cancelling"):
                    return session
            return self.sessions.get(self.order[-1])

    def list(self):
        with self.lock:
            return [self.sessions[sid].status() for sid in reversed(self.order)
                    if sid in self.sessions]

    def stop_all(self):
        for session in list(self.sessions.values()):
            try:
                session.stop()
            except Exception:
                pass

    def cleanup_all(self):
        for session in list(self.sessions.values()):
            try:
                session.cleanup(why="the helper is stopping")
            except Exception:
                pass
        shutil.rmtree(self.mods_dir, ignore_errors=True)

    # ---- the mods a visitor handed over, shared by every session
    def accept_mod(self, name, blob):
        keeper = Session(self.cfg, self.curl, manager=self, sid="keeper")
        return keeper.accept_mod(name, blob)

    def drop_mod(self, name):
        keeper = Session(self.cfg, self.curl, manager=self, sid="keeper")
        return keeper.drop_mod(name)

    def clear_mods(self):
        keeper = Session(self.cfg, self.curl, manager=self, sid="keeper")
        return keeper.clear_mods()

    @property
    def mods(self):
        with self.lock:
            return list(self.pending_mods)


# --------------------------------------------------------------------------- the tunnel
#
# A network that lets nothing out but HTTPS makes a Minecraft server unreachable, because the game
# dials a raw TCP port. The fix is to carry that TCP connection inside a WebSocket to a relay that
# dials the server on the visitor's behalf - which is what Cloudflare Workers can do, and what this
# project's gateway already implements.
#
# The protocol is the one documented in gateway/cloudflare-worker.js ("goldproxy.v1"), so the Worker
# that repository deploys works with this unchanged:
#
#   POST /v1/token {host,port}          -> {"token":…}
#   WS   /v1/connect  {"t":"open",…}    <- {"t":"ready",…}
#   then binary frames are raw TCP, and text frames are
#        {"t":"ack","n"} (flow control), {"t":"ping"/"pong","id"}, {"t":"error",…}, {"t":"end",…}


class WebSocketError(RuntimeError):
    pass


def recv_exact(sock, count, timeout=30):
    """Read exactly this many bytes, or fail. SOCKS has no concept of a short reply."""
    sock.settimeout(timeout)
    out = b""
    while len(out) < count:
        chunk = sock.recv(count - len(out))
        if not chunk:
            raise OSError("the connection closed before it had sent %d bytes" % count)
        out += chunk
    return out


def parse_socks_url(url):
    """socks5h://host:port -> (host, port, resolve_here). h means the proxy resolves names."""
    import urllib.parse as _u
    parts = _u.urlsplit(url if "://" in url else "socks5h://" + url)
    if not parts.hostname:
        raise ValueError("not a proxy address: %r" % url)
    return parts.hostname, int(parts.port or 1080), parts.scheme.lower() == "socks5"


def socks5_connect(proxy_url, host, port, timeout=30):
    """Open a TCP connection to host:port *through* a SOCKS5 proxy that is already running.

    This is the other half of what a relay does: a relay carries the connection over HTTPS for you,
    this hands it to a proxy you already have (a VPN client, Shadowsocks, an SSH -D tunnel). The game
    cannot tell the difference, and neither can the server.
    """
    proxy_host, proxy_port, resolve_here = parse_socks_url(proxy_url)
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        # offer "no authentication" only: the proxies people actually run locally accept it, and a
        # password prompt is not something this helper should be collecting
        sock.sendall(b"\x05\x01\x00")
        greeting = recv_exact(sock, 2, timeout)
        if greeting[0] != 5:
            raise OSError("that is not a SOCKS5 proxy (it answered version %d)" % greeting[0])
        if greeting[1] != 0:
            raise OSError("that proxy wants a username and password, which this helper does not ask for")
        if resolve_here:
            try:
                address = socket.gethostbyname(host)
            except Exception:
                address = None
            if address:
                request = b"\x05\x01\x00\x01" + socket.inet_aton(address) + struct.pack("!H", int(port))
            else:
                request = (b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode()
                           + struct.pack("!H", int(port)))
        else:
            request = (b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode()
                       + struct.pack("!H", int(port)))
        sock.sendall(request)
        reply = recv_exact(sock, 4, timeout)
        if reply[1] != 0:
            reasons = {1: "general failure", 2: "not allowed by that proxy's rules", 3: "network unreachable",
                       4: "host unreachable", 5: "connection refused", 6: "timed out", 7: "unsupported",
                       8: "unsupported address"}
            raise OSError("the proxy refused %s:%s: %s" % (host, port, reasons.get(reply[1], reply[1])))
        if reply[3] == 1:
            recv_exact(sock, 4, timeout)
        elif reply[3] == 4:
            recv_exact(sock, 16, timeout)
        elif reply[3] == 3:
            recv_exact(sock, recv_exact(sock, 1, timeout)[0], timeout)
        recv_exact(sock, 2, timeout)
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


def pump_sockets(left, right, stop, log=None, label="proxy"):
    """Copy bytes between two ordinary sockets until either closes. The non-relay half of bridge()."""
    def copy(source, target):
        try:
            while not stop.is_set():
                data = source.recv(32768)
                if not data:
                    break
                target.sendall(data)
        except Exception:
            pass
        finally:
            stop.set()
    threads = [threading.Thread(target=copy, args=(left, right), daemon=True),
               threading.Thread(target=copy, args=(right, left), daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3600)
    for sock in (left, right):
        try:
            sock.close()
        except Exception:
            pass


class RelayDialer:
    """A dialer whose connections are carried inside a WebSocket to a relay."""

    kind = "relay"

    def __init__(self, relay, log=print, via_proxy=None):
        self.relay = relay.rstrip("/")
        self.log = log
        self.via_proxy = via_proxy
        self._probed = None

    def describe(self):
        return "the relay %s" % self.relay

    def sees(self):
        return "the relay's address"

    def _needs_proxy(self):
        """A relay on a blocked hostname fails fast and for everybody: find out once, before dialling."""
        if self._probed is None:
            self._probed = False
            if self.via_proxy:
                try:
                    parts = urllib.parse.urlsplit(self.relay)
                    probe = socket.create_connection((parts.hostname, parts.port or 443), timeout=4)
                    probe.close()
                except Exception:
                    self._probed = True
                    self.log("relay: %s is not reachable from here; reaching it through %s"
                             % (self.relay, self.via_proxy))
        return self._probed

    def attach(self, client, host, port, on_open=None):
        return bridge(client, self.relay, host, port, self.log, on_open=on_open,
                      via_proxy=self.via_proxy if self._needs_proxy() else None,
                      keep_client=True)


class SocksDialer:
    """A dialer whose connections go through a SOCKS5 proxy the visitor already runs."""

    kind = "socks"

    def __init__(self, proxy_url, log=print):
        self.proxy_url = proxy_url
        self.log = log

    def describe(self):
        return "your own proxy at %s" % self.proxy_url

    def sees(self):
        return "your proxy's address"

    def attach(self, client, host, port, on_open=None):
        try:
            far = socks5_connect(self.proxy_url, host, port)
        except Exception as exc:  # noqa: BLE001
            # Deliberately do not close the client: the caller answers it with a SOCKS failure, which
            # is a sentence Java understands. A closed socket reads as "Malformed reply".
            self.log("proxy: %s:%s could not be opened through %s: %s" % (host, port, self.proxy_url, exc))
            return False
        if on_open:
            on_open()
        stop = threading.Event()
        threading.Thread(target=pump_sockets, args=(client, far, stop, self.log), daemon=True).start()
        return True


class DirectDialer:
    """No proxy: the connection is made from this machine, which is what a normal network wants."""

    kind = "direct"

    def __init__(self, log=print):
        self.log = log

    def describe(self):
        return "a direct connection"

    def sees(self):
        return "your own address"

    def attach(self, client, host, port, on_open=None):
        try:
            far = socket.create_connection((host, int(port)), timeout=30)
        except Exception as exc:  # noqa: BLE001
            self.log("direct: %s:%s could not be reached: %s" % (host, port, exc))
            return False
        if on_open:
            on_open()
        stop = threading.Event()
        threading.Thread(target=pump_sockets, args=(client, far, stop, self.log), daemon=True).start()
        return True


def make_dialer(relay=None, socks=None, log=print, via_proxy=None):
    """One decision, in one place: how does the game's network traffic leave this machine?

    A relay when there is one (it works on networks that block game ports), the visitor's own proxy if
    they have one, and a direct connection otherwise.
    """
    if relay:
        if socks:
            log("game traffic: going through %s, reached via %s" % (relay, socks))
        return RelayDialer(relay, log=log, via_proxy=via_proxy)
    if socks:
        return SocksDialer(socks, log=log)
    return DirectDialer(log=log)


def dialer_from(value, log=print):
    """Accept either a dialer or a bare relay URL, so older callers keep working."""
    if value is None or isinstance(value, (RelayDialer, SocksDialer, DirectDialer)):
        return value
    return RelayDialer(str(value), log=log)


def ws_connect(url, timeout=20, via_proxy=None):
    """The smallest WebSocket client that can carry a byte stream: handshake, frames, ping/pong.

    Python's standard library has no WebSocket client, and this file aims to depend on nothing but
    the standard library, so it does the handshake and the framing itself.
    """
    import ssl
    parts = urllib.parse.urlsplit(url)
    secure = parts.scheme in ("wss", "https")
    host = parts.hostname
    port = parts.port or (443 if secure else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if via_proxy:
        # Reach the relay itself through a proxy this machine already has. Useful when the network
        # blocks the relay's own hostname but a proxy on 127.0.0.1 answers, which is a common shape.
        sock = socks5_connect(via_proxy, host, port, timeout=timeout)
    else:
        sock = socket.create_connection((host, port), timeout=timeout)
    if secure:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    request = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n"
               # the gateway speaks this dialect and refuses anything else, which is a good thing:
               # a relay that accepted any upgrade would be a browser-accessible port scanner
               "Sec-WebSocket-Protocol: goldproxy.v1\r\n\r\n"
               % (path, parts.netloc, key))
    sock.sendall(request.encode())
    # read the response headers, one byte at a time so nothing of the first frame is consumed
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(1)
        if not chunk:
            raise WebSocketError("the relay closed the connection during the handshake")
        head += chunk
        if len(head) > 8192:
            raise WebSocketError("the relay sent no usable handshake")
    status, _, rest = head.decode("utf-8", "replace").partition("\r\n")
    if "101" not in status:
        raise WebSocketError("the relay refused the WebSocket: %s" % status.strip())
    expect = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                                           .encode()).digest()).decode()
    if expect.lower() not in rest.lower():
        raise WebSocketError("the relay's handshake did not match")
    return sock


def ws_send(sock, data, opcode=2):
    """One masked frame. Clients must mask; servers must not."""
    header = bytearray([0x80 | opcode])
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length < (1 << 16):
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    mask = os.urandom(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    with _ws_lock_for(sock):
        sock.sendall(bytes(header) + masked)


def ws_recv(sock, timeout=None):
    """One frame: (opcode, payload). Raises on a closed connection."""
    def exact(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise WebSocketError("closed")
            buf += chunk
        return buf
    if timeout is not None:
        sock.settimeout(timeout)
    first = exact(2)
    opcode = first[0] & 0x0F
    masked = bool(first[1] & 0x80)
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", exact(8))[0]
    mask = exact(4) if masked else None
    payload = exact(length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


_WS_LOCKS = {}


def _ws_lock_for(sock):
    """One lock per socket, so two threads (game->relay, relay->game) never interleave frames."""
    with threading.Lock():
        lock = _WS_LOCKS.get(id(sock))
        if lock is None:
            lock = threading.Lock()
            _WS_LOCKS[id(sock)] = lock
        return lock


def bridge(client, relay, host, port, log, on_open=None, timeout=120, via_proxy=None,
           keep_client=False):
    """Carry one TCP connection through the relay: the piece both the tunnel and the SOCKS proxy need.

    Returns True once the relay said it was ready, False if it could not be reached at all.
    """
    relay_host = relay.rstrip("/")
    try:
        # The token request goes the same way the socket will. Through a proxy when that is the only
        # route to the relay, or it would fail with a connection refused nobody could explain.
        answer = http_json(relay_host.replace("ws://", "http://").replace("wss://", "https://")
                           + "/v1/token", body={"host": host, "port": int(port)},
                           via_proxy=via_proxy, timeout=30)
        token = answer.get("token")
        if not token:
            raise WebSocketError("the relay did not issue a token")
        ws_url = relay_host.replace("https://", "wss://").replace("http://", "ws://") + "/v1/connect"
        sock = ws_connect(ws_url, via_proxy=via_proxy)
        ws_send(sock, json.dumps({"t": "open", "v": 1, "token": token, "host": host,
                                  "port": int(port)}).encode(), opcode=1)
        for _ in range(20):
            opcode, payload = ws_recv(sock)
            if opcode == 9:
                ws_send(sock, payload, opcode=10)
                continue
            if opcode == 8:
                raise WebSocketError("the relay closed the connection")
            if opcode != 1:
                continue
            message = json.loads(payload.decode("utf-8", "replace"))
            if message.get("t") == "ready":
                break
            if message.get("t") == "error":
                raise WebSocketError(message.get("message") or message.get("code") or "refused")
        else:
            raise WebSocketError("the relay never said it was ready")
    except Exception as exc:  # noqa: BLE001
        log("relay: %s:%s could not be reached: %s" % (host, port, exc))
        if not keep_client:
            try:
                client.close()
            except Exception:
                pass
        return False

    if on_open:
        on_open()
    stop = threading.Event()

    def to_relay():
        try:
            while not stop.is_set():
                data = client.recv(32768)
                if not data:
                    break
                ws_send(sock, data)
        except Exception:
            pass
        finally:
            stop.set()
            try:
                ws_send(sock, b"", opcode=8)
            except Exception:
                pass

    threading.Thread(target=to_relay, daemon=True).start()
    received = 0
    try:
        while not stop.is_set():
            opcode, payload = ws_recv(sock, timeout=timeout)
            if opcode in (2, 0):
                client.sendall(payload)
                # flow control is cumulative: the relay replaces its counter with this number
                received += len(payload)
                try:
                    ws_send(sock, json.dumps({"t": "ack", "n": received}).encode(), opcode=1)
                except Exception:
                    pass
            elif opcode == 1:
                message = json.loads(payload.decode("utf-8", "replace"))
                if message.get("t") == "ping":
                    ws_send(sock, json.dumps({"t": "pong", "id": message.get("id")}).encode(), 1)
                elif message.get("t") in ("end", "error"):
                    break
            elif opcode == 9:
                ws_send(sock, payload, opcode=10)
            elif opcode == 8:
                break
    except Exception:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass
    return True


class SocksBridge:
    """A local SOCKS5 proxy whose connections are carried by the relay.

    This is the setting for a network that blocks everything but HTTPS: Java routes *every* TCP
    connection a program makes through `-DsocksProxyHost`, so pointing the game at this proxy is what
    makes joining a server, authenticating and fetching skins work - not just one server chosen in
    advance. It needs no administrator and touches nothing outside the session.
    """

    def __init__(self, dialer, port=0, log=print):
        # A relay URL or any dialer, so the game can be pointed at this proxy whether the way out
        # is a relay, the visitor's own proxy, or a direct connection.
        self.dialer = dialer_from(dialer, log=log)
        self.relay = getattr(self.dialer, "relay", None)
        self.port = int(port or 0)
        self.log = log
        self.server = None
        self.thread = None
        self.running = False
        self.connections = 0
    def start(self):
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", self.port))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        self.running = True

        def accept():
            while self.running:
                try:
                    client, _addr = self.server.accept()
                except OSError:
                    break
                threading.Thread(target=self._handle, args=(client,), daemon=True).start()

        self.thread = threading.Thread(target=accept, daemon=True)
        self.thread.start()
        self.log("proxy: SOCKS5 on 127.0.0.1:%d, every connection carried by %s"
                 % (self.port, self.dialer.describe()))
        return self.port

    def stop(self):
        self.running = False
        try:
            if self.server:
                self.server.close()
        except Exception:
            pass

    def _handle(self, client):
        try:
            greeting = client.recv(2)
            if len(greeting) < 2 or greeting[0] != 5:
                client.close()
                return
            methods = client.recv(greeting[1])
            if 0 not in methods:
                client.sendall(b"\x05\xff")      # no acceptable method
                client.close()
                return
            client.sendall(b"\x05\x00")           # no authentication
            head = client.recv(4)
            if len(head) < 4 or head[0] != 5 or head[1] != 1:
                client.close()
                return
            kind = head[3]
            if kind == 1:
                host = socket.inet_ntoa(client.recv(4))
            elif kind == 3:
                length = client.recv(1)[0]
                host = client.recv(length).decode("utf-8", "replace")
            elif kind == 4:
                host = socket.inet_ntop(socket.AF_INET6, client.recv(16))
            else:
                client.close()
                return
            port = struct.unpack("!H", client.recv(2))[0]
            def opened():
                # The reply the SOCKS client is blocked on. Without it the game waits for an answer
                # that never comes, then reads server bytes as if they were that answer.
                self._count(host, port)
                try:
                    client.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0")
                                   + struct.pack("!H", 0))
                except Exception:
                    pass

            # whichever dialer it is, it says when the far end is up
            ok = self.dialer.attach(client, host, port, on_open=opened)
            if not ok:
                client.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
        except Exception:
            try:
                client.close()
            except Exception:
                pass

    def _count(self, host, port):
        self.connections += 1
        self.log("proxy: %s:%s is open through %s" % (host, port, self.dialer.describe()))
class Tunnel:
    """One Minecraft server, reachable at 127.0.0.1:<local port> through a relay.

    The game connects to the local address as if it were the server; every connection is carried to
    the relay inside a WebSocket, and the relay dials the real server.
    """

    def __init__(self, dialer, host, port, local_port=0, log=print):
        self.dialer = dialer_from(dialer, log=log)
        self.relay = getattr(self.dialer, "relay", None)
        self.host = host
        self.port = int(port)
        self.local_port = int(local_port or 0)
        self.log = log
        self.server = None
        self.thread = None
        self.running = False
        self.connections = 0
    def _token(self):
        import urllib.request
        body = json.dumps({"host": self.host, "port": self.port}).encode()
        req = urllib.request.Request(self.relay.replace("ws://", "http://").replace("wss://", "https://")
                                     + "/v1/token", data=body,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "%s/%s" % (NAME, VERSION)})
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        token = data.get("token")
        if not token:
            raise WebSocketError("the relay did not issue a token: %s" % data)
        return token

    def _pump(self, client):
        """One game connection: open the far end however the dialer does, then copy both ways."""
        self.dialer.attach(client, self.host, self.port, on_open=self._opened)

    def _opened(self):
        self.connections += 1
        self.log("tunnel: %s:%d is open through %s"
                 % (self.host, self.port, self.dialer.describe()))
    def _pump_unused(self, client):
        try:
            token = self._token()
            ws_url = (self.relay.replace("https://", "wss://").replace("http://", "ws://")
                      .rstrip("/") + "/v1/connect")
            sock = ws_connect(ws_url)
            ws_send(sock, json.dumps({"t": "open", "v": 1, "token": token,
                                      "host": self.host, "port": self.port}).encode(), opcode=1)
            # wait for ready, ignoring anything else the relay says first
            for _ in range(20):
                opcode, payload = ws_recv(sock)
                if opcode == 9:
                    ws_send(sock, payload, opcode=10)
                    continue
                if opcode in (8,):
                    raise WebSocketError("the relay closed the connection")
                if opcode != 1:
                    continue
                message = json.loads(payload.decode("utf-8", "replace"))
                if message.get("t") == "ready":
                    break
                if message.get("t") == "error":
                    raise WebSocketError(message.get("message") or message.get("code") or "refused")
            else:
                raise WebSocketError("the relay never said it was ready")
            self.connections += 1
            self.log("tunnel: %s:%d is open through the relay" % (self.host, self.port))
            stop = threading.Event()

            def to_relay():
                try:
                    while not stop.is_set():
                        data = client.recv(32768)
                        if not data:
                            break
                        ws_send(sock, data)
                except Exception:
                    pass
                finally:
                    stop.set()
                    try:
                        ws_send(sock, b"", opcode=8)
                    except Exception:
                        pass

            up = threading.Thread(target=to_relay, daemon=True)
            up.start()
            received = 0
            while not stop.is_set():
                opcode, payload = ws_recv(sock, timeout=120)
                if opcode == 2 or opcode == 0:
                    client.sendall(payload)
                    # The relay's flow control is a *cumulative* count: it pauses the target while
                    # the client has more unacknowledged bytes than the window, and it sets its own
                    # counter from this number rather than adding to it. Sending the size of one
                    # frame here stalls a real session after a few hundred kilobytes.
                    received += len(payload)
                    try:
                        ws_send(sock, json.dumps({"t": "ack", "n": received}).encode(), opcode=1)
                    except Exception:
                        pass
                elif opcode == 1:
                    message = json.loads(payload.decode("utf-8", "replace"))
                    if message.get("t") == "ping":
                        ws_send(sock, json.dumps({"t": "pong", "id": message.get("id")}).encode(), 1)
                    elif message.get("t") in ("end", "error"):
                        break
                elif opcode == 9:
                    ws_send(sock, payload, opcode=10)
                elif opcode == 8:
                    break
        except Exception as exc:  # noqa: BLE001
            self.log("tunnel: %s:%d could not be reached through the relay: %s"
                     % (self.host, self.port, exc))
        finally:
            try:
                client.close()
            except Exception:
                pass

    def start(self):
        if self.dialer is None:
            raise WebSocketError("tunnelling a server needs a relay or a proxy "
                                 "(Settings → If a network blocks things)")
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", self.local_port))
        self.server.listen(4)
        self.local_port = self.server.getsockname()[1]
        self.running = True

        def accept():
            while self.running:
                try:
                    client, _addr = self.server.accept()
                except OSError:
                    break
                threading.Thread(target=self._pump, args=(client,), daemon=True).start()

        self.thread = threading.Thread(target=accept, daemon=True)
        self.thread.start()
        self.log("tunnel: %s:%d will be reachable at 127.0.0.1:%d through %s"
                 % (self.host, self.port, self.local_port, self.dialer.describe()))
        return self.local_port

    def stop(self):
        self.running = False
        try:
            if self.server:
                self.server.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- session

class Session:
    """One sandbox, one game, one lifecycle.

    More than one of these can be alive at once - two windows, two versions - so each session gets
    its own Curl (its own downloads to abort) and its own sandbox, and shares only the mod jars the
    visitor handed over and the proxy/relay settings.
    """

    def __init__(self, cfg, curl, manager=None, sid=None):
        self.cfg = cfg
        # a private Curl, so stopping one session cannot abort another's downloads
        self.curl = Curl(proxy=getattr(curl, "proxy", None))
        self.curl.relay = getattr(cfg, "relay", None)
        self.manager = manager
        self.sid = sid or secrets.token_hex(3)
        self.lock = threading.RLock()
        self.lines = []
        self.state = "idle"          # idle|running|exited|cancelled|failed
        self.phase = ""
        self.phase_label = ""
        self.done = 0
        self.total = 0
        self.error = None
        self.instance = {}
        self.pid = None
        self.exit_code = None
        self.started_at = None
        self.ended_at = None
        self.result = None           # collected bundle info
        self.proc = None
        self.thread = None
        self.cleanup_deadline = None
        self.last_cleanup = None
        self.cancel_requested = False
        self.last_touch = time.time()
        self.pending_mods = []      # jars the page pushed in, waiting for a launch
        self.tunnel = None          # a server carried to us through a relay, when asked for
        self.socks = None           # a local SOCKS5 proxy the game itself is pointed at
        self.dialer = None          # how the game's own connections leave this machine
        self.quick_play = None      # 127.0.0.1:<port>, handed to the client to join
        self.java = None
        self.java_major = 0
        self.jre_fetched = False
        self._versions_cache = None

    # ---------------------------------------------------------------- root paths
    @property
    def root(self):
        return self.cfg.root

    @property
    def game_dir(self):
        return os.path.join(self.root, "game")

    @property
    def libs_dir(self):
        return os.path.join(self.root, "libraries")

    @property
    def assets_dir(self):
        return os.path.join(self.root, "assets")

    @property
    def natives_dir(self):
        return os.path.join(self.root, "natives")

    @property
    def versions_dir(self):
        return os.path.join(self.root, "versions")

    @property
    def bundle_path(self):
        return os.path.join(self.root, "session.zip")

    def write_file(self, rel, data):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    # ---------------------------------------------------------------- status
    def log(self, line, echo=True):
        line = ANSI_RE.sub("", str(line)).rstrip("\n")
        if not line.strip():
            return
        with self.lock:
            self.lines.append("[%s] %s" % (log_ts(), line))
            if len(self.lines) > 4000:
                del self.lines[:1000]
        if echo:
            sys.stdout.write("  %s\n" % line)
            sys.stdout.flush()

    def set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def status(self):
        with self.lock:
            total = self.total or 0
            data = {
                "sid": self.sid,
                "state": self.state,
                "phase": self.phase,
                "phaseLabel": self.phase_label,
                "progress": {
                    "done": self.done,
                    "total": total,
                    "percent": int(100 * self.done / total) if total else None,
                },
                "instance": dict(self.instance),
                "pid": self.pid,
                "exitCode": self.exit_code,
                "startedAt": self.started_at,
                "endedAt": self.ended_at,
                "error": self.error,
                "java": self.java,
                "javaMajor": self.java_major,
                "jreFetched": self.jre_fetched,
                "downloadedBytes": self.curl.bytes_in,
                "lines": len(self.lines),
                "result": self.result,
            }
        self.refresh_cleanup_deadline()
        data["cleanupAt"] = self.cleanup_deadline
        data["lastCleanup"] = self.last_cleanup
        data["pendingMods"] = [{"name": m["name"], "bytes": m["bytes"],
                               "title": m.get("title"), "version": m.get("version")}
                              for m in self.pending_mods]
        # A second tab (or the same visitor later) must be able to tell that another page
        # has already taken this session's files and the sandbox has been deleted, rather
        # than reporting a failed download.
        if data["result"]:
            data["result"] = dict(data["result"])
            data["result"]["available"] = os.path.exists(self.bundle_path)
        return data

    def refresh_cleanup_deadline(self):
        """After the game exits and the bundle exists, the sandbox gets a timer."""
        with self.lock:
            if (self.state in ("exited", "failed") and self.result
                    and self.cleanup_deadline is None and self.cfg.autoclean):
                self.cleanup_deadline = time.time() + self.cfg.autoclean

    def lines_since(self, n):
        with self.lock:
            return self.lines[n:], len(self.lines)

    # ---------------------------------------------------------------- lifecycle
    def start(self, opts):
        with self.lock:
            if self.state == "running":
                raise RuntimeError("a session is already running")
            self.state = "running"
            self.error = None
            self.exit_code = None
            self.result = None
            self.done = 0
            self.total = 0
            self.cleanup_deadline = None
            self.cancel_requested = False
            self.started_at = time.time()
            self.ended_at = None
        self.curl.clear_abort()
        self.thread = threading.Thread(target=self._run, args=(opts,), daemon=True)
        self.thread.start()

    def touch(self):
        """Remember that somebody is still talking to us: the idle exit uses this, so a helper
        nobody is driving disappears on its own instead of lingering as a background process."""
        with self.lock:
            self.last_touch = time.time()

    def _check_cancelled(self):
        if self.cancel_requested or self.curl.aborted():
            raise Cancelled("stopped on request")

    def cancel(self, why="the page asked for it"):
        """Stop a launch that has not started yet: the downloads, then the game.

        Stop used to mean "kill the game process", which did nothing while 430 MB of assets
        were still downloading - so picking the wrong version meant waiting for it. This
        interrupts the downloads, ends the job at its next step, and still collects what the
        attempt produced so the page can keep it.
        """
        with self.lock:
            self.cancel_requested = True
            was_running = self.state == "running"
        killed = self.curl.abort()
        game = self.stop()
        self.log("stop requested: cancelling everything%s%s"
                 % (" (%d download(s) killed)" % killed if killed else "",
                    " (game killed)" if game else ""))
        with self.lock:
            if was_running and not game:
                self.state = "cancelling"
        return {"cancelled": True, "downloads": killed, "game": game,
                "why": why, "state": self.state}

    def stop(self):
        self._stop_tunnel()
        with self.lock:
            proc = self.proc
        if proc and proc.poll() is None:
            self.log("stop requested - sending SIGTERM to pid %d" % proc.pid)
            try:
                proc.terminate()
            except Exception:
                pass
            for _ in range(40):
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
            if proc.poll() is None:
                self.log("still alive - SIGKILL")
                try:
                    proc.kill()
                except Exception:
                    pass
            return True
        return False

    def _progress(self, label, done, total):
        with self.lock:
            self.phase_label = label
            self.done = done
            self.total = total

    def _phase(self, name, label=None, total=0, check_cancel=True):
        # Every step of a launch goes through here, so a stop lands between steps rather
        # than at the end of them. Collecting is the exception: once a stop has been
        # honoured, what the attempt produced still has to be saved, not cancelled again.
        if check_cancel:
            self._check_cancelled()
        with self.lock:
            self.phase = name
            self.phase_label = label or name
            self.done = 0
            self.total = total
        if label:
            self.log(label)

    # ---------------------------------------------------------------- the run
    def _run(self, opts):
        try:
            self.log("sandbox: %s" % self.root)
            self._prepare_root()
            if getattr(self, "pending_restore", None):
                try:
                    self.log("putting your last session back, from %s"
                             % getattr(self, "pending_restore_from", "a saved bundle"))
                    self.restore(self.pending_restore)
                except Exception as exc:  # noqa: BLE001
                    self.log("could not put the previous session back: %s" % exc)
            self._preseed_options()
            mc_version = opts["version"]

            # ---- version metadata, read first because it says which Java the client needs
            # (26.x is compiled for Java 25; handing it Java 21 fails with "class file version 69.0")
            self._phase("metadata", "reading %s from Mojang" % mc_version)
            vjson, vurl = self._version_json(mc_version)
            required = int((vjson.get("javaVersion") or {}).get("majorVersion") or 21)

            # ---- java
            self._phase("java", "looking for a Java runtime")
            if getattr(self.cfg, "restricted", False):
                # Restricted mode: never touch a Java the machine already has. The runtime comes
                # into the sandbox and dies with it, so nothing outside /tmp is read or run.
                self.log("restricted mode: a Java runtime will be fetched into the sandbox "
                         "rather than used from this machine")
                java, major = None, 0
                if self.cfg.java and java_major(self.cfg.java):
                    java, major = self.cfg.java, java_major(self.cfg.java)
            else:
                java, major = pick_java(required, explicit=self.cfg.java, log=self.log)
            if not java or major < required:
                if self.cfg.no_jre_download:
                    raise RuntimeError(
                        "no Java %d found (best: %s) and --no-jre-download was given"
                        % (required, java or "none"))
                if java:
                    self.log("best local Java is %d, this version needs %d - fetching one "
                             "into the sandbox (it dies with the sandbox)" % (major, required))
                java, major = fetch_jre(required, self.root, self.curl, self.log)
                self.jre_fetched = True
            self.java = java
            self.java_major = major
            self.log("java: %s (%d)" % (java, major))

            self.log("client: %s (%d MB)" % (
                vjson["id"], vjson["downloads"]["client"]["size"] // (1 << 20)))

            # ---- vanilla files
            client_jar = self._client_jar(vjson)
            libs = self._libraries(vjson, self.curl)
            assets_ok = self._assets(vjson, opts.get("assets", True))

            # ---- fabric
            loader_kind = (opts.get("loaderKind") or "fabric").lower()
            if loader_kind in ("none", "vanilla"):
                loader_kind = "none"
            loader_ver = opts.get("loader") or "auto"
            profile, loader_ver = self._loader_profile(loader_kind, mc_version, loader_ver)
            loader_libs = self._profile_libraries(profile)
            all_libs = libs + loader_libs

            # ---- mods
            mods = self._mods(opts.get("mods") or [], mc_version, loader_ver)
            local = list(self.cfg.mod_jar or []) + list(opts.get("localMods") or [])
            local += self._pending_mod_paths()
            mods += self._local_mods(local)

            # ---- how the game's traffic leaves this machine, decided once and said out loud
            wants_proxy = bool(opts.get("tunnelEverything") or self.cfg.proxy_game)
            server = (opts.get("tunnelServer") or "").strip()
            game_socks = self.cfg.game_proxy or self.cfg.proxy
            if (wants_proxy or server or self.cfg.relay) and not game_socks:
                found = detect_proxy(None, lambda m: None)
                if found:
                    self.log("a proxy is already running here (%s), so this session will use it" % found)
                    game_socks = found
            self.cfg.relay = resolve_relay(self.cfg.relay, log=self.log, via_proxy=game_socks)
            relay_via = _RELAY_CHOICE.get("via") or (game_socks if self.cfg.relay else None)
            self.dialer = make_dialer(relay=self.cfg.relay, socks=game_socks, log=self.log,
                                      via_proxy=relay_via)

            if wants_proxy:
                if self.dialer.kind == "direct":
                    self.log("the game would connect directly: give it a relay or a proxy first "
                             "(Settings → If a network blocks things)")
                else:
                    try:
                        self.socks = SocksBridge(self.dialer, log=self.log)
                        self.socks_port = self.socks.start()
                        self.log("the game's own connections will go through %s; the server will see %s"
                                 % (self.dialer.describe(), self.dialer.sees()))
                    except Exception as exc:  # noqa: BLE001
                        self.log("could not start the proxy: %s" % exc)
                        self.socks = None

            # ---- one server, carried to us, if one was asked for
            if server:
                host, _sep, port = server.rpartition(":")
                if not host or not port.isdigit():
                    self.log("that server address is not host:port - ignoring %r" % server)
                else:
                    try:
                        # servers are often a signpost: the name in the handshake stays the same, but
                        # the connection has to go where the SRV record points
                        real = minecraft_srv(host, via_proxy=_RELAY_CHOICE.get("via") or self.cfg.proxy,
                                             log=self.log)
                        dial_host, dial_port = (real[0], real[1]) if real else (host, int(port))
                        self.tunnel = Tunnel(self.dialer, dial_host, dial_port,
                                             local_port=int(opts.get("tunnelPort") or 0),
                                             log=self.log)
                        local = self.tunnel.start()
                        self.quick_play = "127.0.0.1:%d" % local
                        self.log("the game will join %s; that is carried to %s:%s through %s, so the "
                                 "server will see %s" % (self.quick_play, host, port,
                                                         self.dialer.describe(), self.dialer.sees()))
                    except Exception as exc:  # noqa: BLE001
                        self.log("could not tunnel %s: %s" % (server, exc))
                        self.tunnel = None

            # ---- command
            self._phase("launch", "starting the game")
            cmd, classpath = self._build_command(
                vjson, profile, client_jar, all_libs, mc_version, loader_ver,
                opts.get("username") or "Player", opts.get("memory") or DEFAULT_MEMORY,
                mods)
            self.write_file("launch-command.txt",
                            "\n".join(shell_quote(c) for c in cmd) + "\n")
            if self.cfg.dry_run:
                self.log("dry run: command written to %s/launch-command.txt"
                         % self.root)
                self.log("dry run: not starting the game")
                self.instance = {
                    "version": mc_version, "loader": "fabric-%s" % loader_ver,
                    "mods": [m["name"] for m in mods],
                    "username": opts.get("username") or "Player",
                    "memory": opts.get("memory") or DEFAULT_MEMORY, "offline": True,
                }
                with self.lock:
                    self.state = "exited"
                    self.exit_code = 0
                    self.phase = "dry-run"
                    self.phase_label = "dry run complete"
                    self.ended_at = time.time()
                self._collect()
                return
            self.log("classpath: %d entries" % len(classpath))
            self.log("mods: %s" % (", ".join(m["name"] for m in mods) if mods else "(none)"))
            self.log("assets: %s" % ("downloaded" if assets_ok else "SKIPPED (missing sounds/textures)"))

            logfile = open(os.path.join(self.root, "console.log"), "wb")
            self.proc = subprocess.Popen(cmd, cwd=self.game_dir, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            self.pid = self.proc.pid
            self.instance = {
                "version": mc_version,
                "loader": ("%s-%s" % (loader_kind, loader_ver)) if loader_kind != "none" else "vanilla",
                "mods": [m["name"] for m in mods],
                "username": opts.get("username") or "Player",
                "memory": opts.get("memory") or DEFAULT_MEMORY,
                "offline": True,
            }
            self.log("pid %d - minecraft is running; close the window when you are done" % self.pid)
            with self.lock:
                self.state = "running"
            sink = threading.Thread(target=self._pump, args=(self.proc.stdout, logfile),
                                    daemon=True)
            sink.start()
            code = self.proc.wait()
            sink.join(timeout=10)
            logfile.close()
            self.exit_code = code
            self.log("game exited with code %s" % code)
            self._collect()
            with self.lock:
                self.state = "cancelled" if code not in (0, None) and code < 0 else "exited"
                self.phase = "done"
                self.phase_label = "session finished"
                self.ended_at = time.time()
        except Cancelled as exc:
            self.log("stopped: %s" % exc)
            try:
                # Even a stopped attempt keeps its log: that is the difference between
                # "it didn't work" and "it didn't work and nobody knows why".
                self._collect()
            except Exception as collect_problem:  # noqa: BLE001
                self.log("nothing could be collected: %s" % collect_problem)
            with self.lock:
                self.state = "cancelled"
                self.phase = "cancelled"
                self.phase_label = "Stopped"
                self.ended_at = time.time()
        except Exception as exc:  # noqa: BLE001 - surfaced to the page
            if isinstance(exc, CurlError) and str(exc) == "cancelled":
                with self.lock:
                    self.state = "cancelled"
                    self.phase = "cancelled"
                    self.phase_label = "Stopped"
                    self.ended_at = time.time()
                self.log("stopped on request")
                try:
                    self._collect()
                except Exception:  # noqa: BLE001
                    pass
            else:
                self.error = "%s: %s" % (type(exc).__name__, exc)
                self.log("FAILED - %s" % self.error)
                with self.lock:
                    self.state = "failed"
                    self.phase = "failed"
                    self.phase_label = "failed"
                    self.ended_at = time.time()

        finally:
            # Whatever happened, the sandbox now has a deadline: if the page never collects the
            # result, the watchdog deletes it.
            self.refresh_cleanup_deadline()

    def _pump(self, stream, logfile):
        interesting = re.compile(
            r"Loading \d+ mods|fabric-loader|KnotClient|LWJGL|Backend library|Setting user|"
            r"OpenGL|Sound engine|Started|Stopping|Exception|Error|crash|Mixin|"
            r"Preparing|Reloading|thread", re.I)
        for raw in iter(stream.readline, b""):
            logfile.write(raw)
            logfile.flush()
            text = ANSI_RE.sub("", raw.decode("utf-8", "replace"))
            if interesting.search(text):
                self.log(text, echo=False)
            else:
                with self.lock:
                    self.lines.append("[%s] %s" % (log_ts(), text.rstrip()))

    # ---------------------------------------------------------------- sandbox
    def _prepare_root(self):
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        for d in (self.game_dir, self.libs_dir, self.assets_dir,
                  os.path.join(self.assets_dir, "indexes"),
                  os.path.join(self.assets_dir, "objects"),
                  self.natives_dir, self.versions_dir,
                  os.path.join(self.game_dir, "mods"),
                  os.path.join(self.game_dir, "saves")):
            os.makedirs(d, exist_ok=True)
        self.curl.workdir = self.root
        if getattr(self.cfg, "relay", None):
            # Everything downloaded this session goes through the relay instead of straight to
            # Mojang - which is the point of it on a network that blocks Mojang.
            self.curl.relay = self.cfg.relay
            self.log("relay: every download goes through %s" % self.cfg.relay)
        self._write_owner()
        self._link_cache()

    def _write_owner(self):
        """A note to whoever finds this directory later: who made it, and when.

        If this helper is ever killed outright (SIGKILL, a crash, the machine going
        down) the sandbox is left behind with nobody to delete it. The next helper to
        start reads this, sees that the owning process is gone, and deletes it there
        and then - rather than waiting for an arbitrary age to pass.
        """
        data = {
            "pid": os.getpid(),
            "started": time.time(),
            "helper": VERSION,
            "argv": sys.argv[1:],
            "boot": _boot_id(),
        }
        try:
            with open(os.path.join(self.root, "owner.json"), "w") as fh:
                json.dump(data, fh, indent=1)
        except OSError:
            pass

    def _link_cache(self):
        """Optional: borrow Mojang's immutable files from a cache instead of
        re-downloading 450 MB every time. Off by default - the promise is that a
        session leaves nothing behind, and only the visitor can decide to trade
        that for speed. The sandbox symlinks the cache; deleting the sandbox
        unlinks them and leaves the cache alone."""
        if not self.cfg.cache:
            return
        cache = os.path.abspath(os.path.expanduser(self.cfg.cache))
        for name in ("assets", "libraries"):
            src = os.path.join(cache, name)
            dst = os.path.join(self.root, name)
            os.makedirs(os.path.join(src, "indexes"), exist_ok=True)
            os.makedirs(os.path.join(src, "objects"), exist_ok=True)
            if os.path.islink(dst):
                os.unlink(dst)
            elif os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            elif os.path.exists(dst):
                os.unlink(dst)
            os.symlink(src, dst)
        # the client jar and the version json are immutable too, so they cache by
        # name: a second launch then needs no network for them at all
        cver = os.path.join(cache, "versions")
        os.makedirs(cver, exist_ok=True)
        for entry in os.listdir(cver):
            src = os.path.join(cver, entry)
            dst = os.path.join(self.versions_dir, entry)
            if os.path.exists(dst) or os.path.islink(dst):
                continue
            os.symlink(src, dst)
        self.log("cache: assets, libraries and the client jar are borrowed from %s" % cache)

    def _version_json(self, version):
        cache = os.path.join(self.versions_dir, "%s.json" % version)
        if os.path.exists(cache):
            with open(cache) as fh:
                data = json.load(fh)
            return data, data.get("_url", "")
        manifest = self.curl.json(MANIFEST_URL, headers=self._ua())
        rels = [v for v in manifest["versions"] if v["type"] == "release"]
        match = None
        for v in manifest["versions"]:
            if v["id"] == version:
                match = v
                break
        if not match:
            near = ", ".join(v["id"] for v in (rels or manifest["versions"])[:8])
            raise RuntimeError("version %s is not in Mojang's list (newest releases: %s)"
                               % (version, near))
        data = self.curl.json(match["url"], headers=self._ua())
        data["_url"] = match["url"]
        os.makedirs(self.versions_dir, exist_ok=True)
        with open(cache, "w") as fh:
            json.dump(data, fh)
        return data, match["url"]

    def versions(self):
        with self.lock:
            if self._versions_cache:
                return self._versions_cache
        manifest = self.curl.json(MANIFEST_URL, headers=self._ua())
        rel = [v["id"] for v in manifest["versions"] if v["type"] == "release"][:40]
        allv = [v["id"] for v in manifest["versions"]][:40]
        out = {"latest": manifest["latest"], "releases": rel, "newest": allv}
        with self.lock:
            self._versions_cache = out
        return out

    def _client_jar(self, vjson):
        dl = vjson["downloads"]["client"]
        dest = os.path.join(self.versions_dir, "%s" % dl.get("path", "client.jar"))
        if os.path.isdir(dest):
            dest = os.path.join(dest, "client.jar")
        self._phase("client", "client jar (%.0f MB)" % (dl["size"] / 1048576.0))
        self.curl.get(dl["url"], dest=dest, sha1=dl.get("sha1"), headers=self._ua())
        self._progress("client jar", 1, 1)
        return dest

    def _libraries(self, vjson, curl):
        features = self._features()
        wanted = []
        natives = []
        for lib in vjson["libraries"]:
            if not rules_allow(lib.get("rules"), features):
                continue
            art = lib.get("downloads", {}).get("artifact")
            if art:
                wanted.append((art["url"], os.path.join(self.libs_dir, art["path"]),
                               art.get("sha1"), lib.get("name", "")))
            elif lib.get("name"):
                # no explicit downloads block: build it from maven coordinates
                rel, _name = maven_to_path(lib["name"])
                if rel:
                    repo = (lib.get("url") or "https://libraries.minecraft.net").rstrip("/")
                    wanted.append(("%s/%s" % (repo, rel), os.path.join(self.libs_dir, rel),
                                   None, lib.get("name", "")))
            if lib.get("natives"):
                natives.append(lib)
        # Apple Silicon: Mojang lists both natives-macos and natives-macos-arm64 with
        # identical rules, so both would land on the classpath and LWJGL could extract
        # the x86_64 dylib. Prefer the arm64 variant and drop the plain one.
        wanted = self._prefer_arch(wanted)
        self._phase("libraries", "libraries (%d)" % len(wanted), len(wanted))
        failed = curl.batch([(u, d, s) for u, d, s, _n in wanted],
                            progress=self._progress_cb("libraries"), headers=self._ua())
        if failed:
            self.log("WARNING: %d libraries failed: %s" % (
                len(failed), "; ".join(os.path.basename(f[1]) for f in failed[:4])))
        paths = [d for _u, d, _s, _n in wanted if os.path.exists(d)]
        # classic natives (pre-1.19): extract into natives/
        for lib in natives:
            art = lib.get("downloads", {}).get("classifiers", {}) or {}
            key = lib["natives"].get(os_name())
            if key and key in art:
                entry = art[key]
                jar = os.path.join(self.libs_dir, entry["path"])
                try:
                    curl.get(entry["url"], dest=jar, sha1=entry.get("sha1"))
                    self._extract_natives(jar, self.natives_dir, lib.get("extract"))
                    paths.append(jar)
                except CurlError as exc:
                    self.log("native %s failed: %s" % (lib.get("name"), exc))
        return paths

    def _prefer_arch(self, wanted):
        if os_name() != "osx" or os_arch() != "arm64":
            return wanted
        out = []
        bases = set()
        for url, dest, sha1, name in wanted:
            if ":natives-macos-arm64" in name or "natives-macos-arm64" in url:
                bases.add(name.split(":natives-macos-arm64")[0])
        for url, dest, sha1, name in wanted:
            if "natives-macos" in name and "natives-macos-arm64" not in name:
                base = name.split(":natives-macos")[0]
                if base in bases:
                    continue  # arm64 variant exists; skip x86_64
            out.append((url, dest, sha1, name))
        return out

    def _extract_natives(self, jar, dest, extract):
        exclude = (extract or {}).get("exclude", []) or []
        with zipfile.ZipFile(jar) as zf:
            for info in zf.namelist():
                if info.endswith("/"):
                    continue
                if any(info.startswith(e) for e in exclude):
                    continue
                target = os.path.join(dest, info)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                if info.endswith(".dylib") or info.endswith(".so") or info.endswith(".dll"):
                    os.chmod(target, 0o755)

    def _assets(self, vjson, download=True):
        idx = vjson["assetIndex"]
        idx_path = os.path.join(self.assets_dir, "indexes", "%s.json" % idx["id"])
        self._phase("assets", "asset index %s" % idx["id"])
        self.curl.get(idx["url"], dest=idx_path, sha1=idx.get("sha1"), headers=self._ua())
        if not download:
            self.log("asset download skipped on request")
            return False
        with open(idx_path) as fh:
            data = json.load(fh)
        objects = data["objects"]
        items = []
        for name, obj in objects.items():
            h = obj["hash"]
            items.append(("%s/%s/%s" % (RESOURCES_URL, h[:2], h),
                          os.path.join(self.assets_dir, "objects", h[:2], h), h))
        mb = sum(o["size"] for o in objects.values()) / 1048576.0
        self._phase("assets", "assets (%d objects, %.0f MB)" % (len(items), mb), len(items))
        t0 = time.time()
        failed = self.curl.batch(items, progress=self._progress_cb("assets"),
                                 headers=self._ua())
        if failed:
            # Missing assets are survivable: the game runs with missing textures.
            self.log("WARNING: %d assets missing (~%s). The game will still start." % (
                len(failed), "; ".join(os.path.basename(f[1]) for f in failed[:3])))
        self.log("assets done in %.0fs" % (time.time() - t0))
        return True

    def _fabric_profile(self, mc_version, loader):
        """Fabric hands a profile over directly, and it is the one loader with a stable list to
        choose from - so 'stable' means something here, where Forge and NeoForge just take the
        newest build for the version."""
        text = str(loader).lower()
        want = None if text in ("stable", "latest", "auto", "newest", "", "none") \
            else str(loader).lstrip("fabric-loader-")
        cache_file = self._loader_cache_path("fabric", mc_version, want) if (want and self.cfg.cache) else None
        if cache_file and os.path.exists(cache_file):
            with open(cache_file) as fh:
                prof = json.load(fh)
            self.log("fabric loader %s for %s (from the cache; no network needed)"
                     % (prof.get("_loader"), mc_version))
            return prof, prof.get("_loader")
        try:
            meta = self.curl.json("%s/versions/loader/%s" % (FABRIC_META, mc_version),
                                  headers=self._ua())
        except CurlError:
            if want is None:
                cached = self._newest_cached_profile("fabric", mc_version)
                if cached:
                    prof, lv = cached
                    self.log("fabric: meta.fabricmc.net is unreachable, so the cached loader %s "
                             "is being used for %s" % (lv, mc_version))
                    return prof, lv
            raise
        if not meta:
            raise RuntimeError("Fabric has no loader for Minecraft %s" % mc_version)
        if want is None:
            stable = [e for e in meta if e["loader"].get("stable")]
            pick = meta[0] if text == "latest" else (stable[0] if stable else meta[0])
        else:
            pick = None
            for entry in meta:
                if entry["loader"]["version"] == want:
                    pick = entry
                    break
            if not pick:
                raise RuntimeError("Fabric loader %s does not exist for %s (newest: %s)"
                                   % (want, mc_version,
                                      ", ".join(e["loader"]["version"] for e in meta[:5])))
        version = pick["loader"]["version"]
        self.log("fabric loader %s for %s%s" % (version, mc_version,
                 "" if pick["loader"].get("stable") else " (not marked stable)"))
        profile = self.curl.json("%s/versions/loader/%s/%s/profile/json"
                                 % (FABRIC_META, mc_version, version), headers=self._ua())
        profile["_loader"] = version
        profile["_kind"] = "fabric"
        path = self._loader_cache_path("fabric", mc_version, version)
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                json.dump(profile, fh, indent=1)
        return profile, version

    def _newest_cached_profile(self, kind, mc_version):
        """The loader profile from the most recent cached launch of this version, so a machine
        with no network can still start what it started before."""
        if not self.cfg.cache:
            return None
        cache = os.path.abspath(os.path.expanduser(self.cfg.cache))
        directory = os.path.join(cache, kind)
        if not os.path.isdir(directory):
            return None
        best = None
        for name in os.listdir(directory):
            if not name.startswith(mc_version + "-") or not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            try:
                with open(path) as fh:
                    prof = json.load(fh)
                stamp = os.path.getmtime(path)
            except (OSError, ValueError):
                continue
            if best is None or stamp > best[0]:
                best = (stamp, prof)
        if not best:
            return None
        prof = best[1]
        return prof, prof.get("_loader")

    def _loader_profile(self, kind, mc_version, loader):
        """Resolve a loader for a version, as a version document.

        Fabric hands one over directly; Forge and NeoForge publish the same shape inside their
        installer jar, so all three arrive here as {mainClass, libraries, arguments} and the
        rest of the launch path does not care which one it is.
        """
        if kind in ("none", "vanilla", "", None):
            return None, None
        if kind == "fabric":
            return self._fabric_profile(mc_version, loader)
        if kind not in LOADER_KINDS:
            raise RuntimeError("unknown loader %r (known: %s)" % (kind, ", ".join(LOADER_KINDS)))
        if kind not in SUPPORTED_LOADERS:
            raise RuntimeError(
                "%s is not supported yet: its build resolves and its libraries download, but the "
                "client jar it needs (client-<mc>-<stamp>-srg.jar) is produced by its own "
                "installertools patch step, which this helper does not run. Use "
                "--loader-kind fabric - which is the loader most mods are published for - or "
                "watch the repository for this." % kind)

        want = None if str(loader).lower() in ("auto", "latest", "newest", "stable", "", "none") \
            else str(loader)
        cached = self._loader_cache_path(kind, mc_version, want) if (want and self.cfg.cache) else None
        if cached and os.path.exists(cached):
            with open(cached) as fh:
                prof = json.load(fh)
            self.log("%s %s for %s (from the cache; no network needed)"
                     % (kind, prof.get("_loader"), mc_version))
            return prof, prof.get("_loader")

        try:
            versions = self._loader_versions(kind, mc_version)
        except CurlError as exc:
            if want:
                raise RuntimeError(
                    "could not ask %s which builds exist for %s (%s). With --cache and a "
                    "previous launch of this exact loader build, it can be reused offline."
                    % (kind, mc_version, exc))
            raise
        if not versions:
            raise RuntimeError("%s has no build for Minecraft %s" % (kind, mc_version))
        version = want or versions[-1]
        if want and want not in versions:
            raise RuntimeError("%s %s does not exist for %s (newest: %s)"
                               % (kind, want, mc_version, ", ".join(versions[-3:])))
        profile = self._loader_from_installer(kind, mc_version, version)
        path = self._loader_cache_path(kind, mc_version, version)
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                json.dump(profile, fh, indent=1)
        return profile, version

    def _loader_versions(self, kind, mc_version):
        """Newest last, for this Minecraft version. NeoForge numbers itself 21.11.x for 1.21.11
        and 26.3.x for 26.3, so the mapping is not always the obvious one."""
        xml = self.curl.get(LOADER_METADATA[kind], dest=None, timeout=120).decode("utf-8", "replace")
        everything = re.findall(r"<version>([^<]+)</version>", xml)
        if kind == "forge":
            # forge is <minecraft>-<forge>
            return [v.split("-", 1)[1] for v in everything if v.startswith(mc_version + "-")]
        prefix = (".".join(mc_version.split(".")[1:]) if mc_version.startswith("1.")
                  else ".".join(mc_version.split(".")[:2])) + "."
        return [v for v in everything if v.startswith(prefix)]

    def _loader_installer_url(self, kind, mc_version, version):
        if kind == "forge":
            return "%snet/minecraftforge/forge/%s-%s/forge-%s-%s-installer.jar" % (
                LOADER_MAVEN[kind], mc_version, version, mc_version, version)
        return "%snet/neoforged/neoforge/%s/neoforge-%s-installer.jar" % (
            LOADER_MAVEN[kind], version, version)

    def _loader_from_installer(self, kind, mc_version, version):
        url = self._loader_installer_url(kind, mc_version, version)
        dest = os.path.join(self.versions_dir, "%s-installer-%s.jar" % (kind, version))
        self.log("%s %s for %s: reading its own version document" % (kind, version, mc_version))
        self.curl.get(url, dest=dest, timeout=600, headers=self._ua())
        try:
            with zipfile.ZipFile(dest) as zf:
                if "version.json" not in zf.namelist():
                    raise RuntimeError("%s %s has no version.json to read" % (kind, version))
                profile = json.loads(zf.read("version.json").decode("utf-8", "replace"))
        except zipfile.BadZipFile:
            raise RuntimeError("%s %s did not download as a jar" % (kind, version))
        profile["_loader"] = version
        profile["_kind"] = kind
        libs = profile.get("libraries") or []
        if not libs:
            raise RuntimeError("%s %s lists no libraries" % (kind, version))
        self.log("  %s %s: %s, %d libraries, %s"
                 % (kind, version, profile.get("mainClass"), len(libs),
                    "inherits " + str(profile.get("inheritsFrom"))))
        return profile

    def _loader_cache_path(self, kind, mc_version, version):
        if not self.cfg.cache or not version:
            return None
        cache = os.path.abspath(os.path.expanduser(self.cfg.cache))
        return os.path.join(cache, kind, "%s-%s.json" % (mc_version, version))

    def _profile_libraries(self, profile):
        """Every library a loader profile names, into the sandbox.

        Forge and NeoForge carry full download blocks (url + sha1 + path); Fabric carries only
        Maven coordinates, so the path is built from those. All three end up in the same place.
        """
        if not profile:
            return []
        kind = profile.get("_kind") or "fabric"
        if kind in ("forge", "neoforge"):
            # Forge and NeoForge do not ship a usable client jar: their own installer runs
            # installertools over the vanilla jar to produce client-<mc>-<stamp>-srg.jar and
            # ...-extra.jar, and their bootstrap refuses to start without them. Until the helper
            # runs that step itself, say so here rather than letting FML report a "corrupted
            # installation" twenty seconds later.
            self.log("  %s needs its own client-patching step (installertools), which this helper "
                     "does not run yet - the game will most likely refuse to start." % kind)
        out = []
        for lib in profile.get("libraries", []):
            if not rules_allow(lib.get("rules"), self._features()):
                continue
            artifact = (lib.get("downloads") or {}).get("artifact") or {}
            rel = artifact.get("path")
            url = artifact.get("url")
            sha1 = artifact.get("sha1")
            if rel and url:
                dest = os.path.join(self.libs_dir, rel)
            else:
                name = lib.get("name") or ""
                repo = (lib.get("url") or "").rstrip("/")
                if not repo:
                    if name.startswith("net.minecraftforge"):
                        repo = LOADER_MAVEN["forge"].rstrip("/")
                    elif name.startswith("net.neoforged"):
                        repo = LOADER_MAVEN["neoforge"].rstrip("/")
                    else:
                        repo = "https://libraries.minecraft.net"
                rel, _n = maven_to_path(name)
                if not rel:
                    continue
                dest = os.path.join(self.libs_dir, rel)
                url = "%s/%s" % (repo, rel)
            out.append(dest)
            if not os.path.exists(dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                try:
                    self.curl.get(url, dest=dest, sha1=sha1, headers=self._ua())
                except CurlError as exc:
                    self.log("library %s could not be fetched: %s" % (os.path.basename(dest), exc))
        if out:
            self._phase(kind, "%s %s libraries" % (kind, profile.get("_loader")),
                        len(profile.get("libraries", [])))
            self._progress("%s libraries" % kind, len(out), len(out))
        return out

    def _features(self):
        return {
            "is_demo_user": False,
            "has_custom_resolution": False,
            "has_quick_plays_support": False,
            "is_quick_play_singleplayer": False,
            "is_quick_play_multiplayer": False,
            "is_quick_play_realms": False,
        }

    def _local_mods(self, paths):
        """Jars already on this machine, copied into the sandbox's mods folder.
        These are deleted with everything else when the session ends."""
        out = []
        mods_dir = os.path.join(self.game_dir, "mods")
        for p in paths:
            src = os.path.abspath(os.path.expanduser(p))
            if not os.path.exists(src):
                self.log("mod jar %s does not exist - skipped" % src)
                continue
            if not src.endswith(".jar"):
                self.log("mod jar %s is not a .jar - skipped" % src)
                continue
            dest = os.path.join(mods_dir, os.path.basename(src))
            shutil.copy2(src, dest)
            self.log("mod %s (local file, copied into the sandbox)" % os.path.basename(src))
            out.append({"name": os.path.basename(src), "slug": "local",
                        "version": "local file", "file": os.path.basename(src),
                        "path": dest})
        return out

    def _mods(self, slugs, mc_version, loader_ver):
        out = []
        if not slugs:
            return out
        self._phase("mods", "mods from Modrinth (%d)" % len(slugs), len(slugs))
        mods_dir = os.path.join(self.game_dir, "mods")
        for i, slug in enumerate(slugs):
            try:
                q = urllib.parse.urlencode({
                    "loaders": json.dumps(["fabric"]),
                    "game_versions": json.dumps([mc_version]),
                })
                versions = self.curl.json("%s/project/%s/version?%s" % (MODRINTH_API, slug, q),
                                          headers=self._ua())
                if not versions:
                    self.log("mod %s has no Fabric build for %s - skipped" % (slug, mc_version))
                    self._progress("mods", i + 1, len(slugs))
                    continue
                v = versions[0]
                files = [f for f in v["files"] if f.get("primary")] or v["files"]
                f = files[0]
                dest = os.path.join(mods_dir, f["filename"])
                self.curl.get(f["url"], dest=dest, headers=self._ua())
                self.log("mod %s %s -> %s" % (v.get("name") or slug, v["version_number"],
                                              f["filename"]))
                out.append({"name": v.get("name") or slug, "slug": slug,
                            "version": v["version_number"], "file": f["filename"],
                            "path": dest})
            except CurlError as exc:
                self.log("mod %s could not be fetched: %s" % (slug, exc))
            self._progress("mods", i + 1, len(slugs))
        return out

    def _build_command(self, vjson, profile, client_jar, libs, mc_version, loader_ver,
                       username, memory, mods):
        features = self._features()
        if profile:
            main_class = profile.get("mainClass") or vjson["mainClass"]
            version_name = profile.get("id") or ("fabric-loader-%s-%s" % (loader_ver, mc_version))
            jvm_args = list(vjson["arguments"]["jvm"])
            for a in profile.get("arguments", {}).get("jvm", []) or []:
                if isinstance(a, str) or rules_allow(a.get("rules"), features):
                    jvm_args.append(a)
            game_args = profile.get("arguments", {}).get("game") or vjson["arguments"]["game"]
            version_type = "Fabric"
        else:
            main_class = vjson["mainClass"]
            version_name = vjson["id"]
            jvm_args = list(vjson["arguments"]["jvm"])
            game_args = vjson["arguments"]["game"]
            version_type = vjson.get("type", "release")

        classpath = list(libs)
        if client_jar not in classpath:
            classpath.append(client_jar)
        cp = os.pathsep.join(classpath)
        offline_uuid = offline_uuid_for(username)
        sub = {
            "auth_player_name": username,
            "version_name": version_name,
            "game_directory": self.game_dir,
            "assets_root": self.assets_dir,
            "assets_index_name": vjson["assetIndex"]["id"],
            "auth_uuid": offline_uuid,
            "auth_access_token": "0",
            "auth_session": "0",
            "user_type": "legacy",
            "version_type": version_type,
            "user_properties": "{}",
            "clientid": "",
            "auth_xuid": "",
            "launcher_name": NAME,
            "launcher_version": VERSION,
            "natives_directory": self.natives_dir,
            "library_directory": self.libs_dir,
            "classpath": cp,
            "classpath_separator": os.pathsep,
            "resolution_width": "1280",
            "resolution_height": "720",
            "quickPlayPath": "",
            "quickPlaySingleplayer": "",
            "quickPlayMultiplayer": "",
            "quickPlayRealms": "",
        }
        cmd = [self.java, "-Xmx%s" % memory]
        if getattr(self, "socks", None):
            # Java routes every TCP connection a program makes through these, so the game's own
            # multiplayer, authentication and skin fetching all travel inside the relay
            cmd += ["-DsocksProxyHost=127.0.0.1", "-DsocksProxyPort=%d" % self.socks_port,
                    "-Djava.net.useSystemProxies=false"]
        if os_name() == "osx":
            cmd += ["-Dapple.awt.application.appearance=system"]
        cmd += ["-Dfabric.gameJarPath=%s" % client_jar]
        cmd += materialise(jvm_args, sub, features)
        cmd += [main_class]
        cmd += materialise(game_args, sub, features)
        if self.quick_play:
            # 1.20 and later can join straight away: the difference between "connect to
            # 127.0.0.1:35565 yourself" and pressing PLAY and already being in the server
            cmd += ["--quickPlayMultiplayer", self.quick_play]
        return cmd, classpath

    def _preseed_options(self):
        """A brand new session has no options.txt, and Minecraft's title screen then asks whether to
        turn the narrator on - every single time, for somebody who never wanted it. One line fixes
        that; anything restored from a previous session still wins, because this writes only when
        there is nothing there."""
        path = os.path.join(self.game_dir, "options.txt")
        if os.path.exists(path):
            return
        try:
            with open(path, "w") as fh:
                fh.write("narrator:0\n")
            self.log("wrote a starter options.txt (narrator off, so the title screen does not ask)")
        except OSError:
            pass

    def _collect(self):
        self._phase("collect", "collecting what the session wrote", check_cancel=False)
        wanted = ["saves", "logs", "crash-reports", "screenshots", "config",
                  "options.txt", "servers.dat", "usercache.json", "realms_persistence.json"]
        files = []
        with zipfile.ZipFile(self.bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in wanted:
                src = os.path.join(self.game_dir, rel)
                if not os.path.exists(src):
                    continue
                if os.path.isfile(src):
                    zf.write(src, rel)
                    files.append({"path": rel, "size": os.path.getsize(src)})
                    continue
                for base, _dirs, names in os.walk(src):
                    for n in names:
                        full = os.path.join(base, n)
                        arc = os.path.relpath(full, self.game_dir)
                        try:
                            zf.write(full, arc)
                            files.append({"path": arc, "size": os.path.getsize(full)})
                        except OSError:
                            pass
            # the console log is ours, and it is the thing that explains a crash
            console = os.path.join(self.root, "console.log")
            if os.path.exists(console):
                zf.write(console, "console.log")
                files.append({"path": "console.log", "size": os.path.getsize(console)})
            # The helper's own account of the attempt, always. A launch that was stopped (or that
            # died) before the game started has no console.log at all, and this is then the only
            # record of what it was doing - which is the whole difference between "it didn't work"
            # and "it didn't work and nobody knows why".
            with self.lock:
                helper_lines = list(self.lines)
            if helper_lines:
                payload = ("\n".join(helper_lines) + "\n").encode("utf-8")
                zf.writestr("helper.log", payload)
                files.append({"path": "helper.log", "size": len(payload)})
            launch = os.path.join(self.root, "launch-command.txt")
            if os.path.exists(launch):
                zf.write(launch, "launch-command.txt")
                files.append({"path": "launch-command.txt", "size": os.path.getsize(launch)})
        total = os.path.getsize(self.bundle_path)
        worlds = sorted({f["path"].split("/")[1] for f in files
                         if f["path"].startswith("saves/") and len(f["path"].split("/")) > 2})
        self.result = {
            "bundle": "session.zip",
            "bytes": total,
            "files": len(files),
            "worlds": worlds,
            "hasCrash": any(f["path"].startswith("crash-reports/") for f in files),
            "exitCode": self.exit_code,
            "collectedAt": time.time(),
        }
        if self.manager:
            try:
                self.manager.store_session(self)
            except Exception as exc:  # noqa: BLE001 - keeping it is best-effort
                self.log("could not keep this session for next time: %s" % exc)
        self.log("collected %d files (%.1f MB) into session.zip%s" % (
            len(files), total / 1048576.0,
            " - worlds: " + ", ".join(worlds) if worlds else ""))
        return self.result



    def _stop_tunnel(self):
        if getattr(self, "socks", None):
            try:
                self.socks.stop()
            except Exception:
                pass
            self.socks = None
        if self.tunnel:
            try:
                self.tunnel.stop()
            except Exception:
                pass
            self.tunnel = None

    def cleanup(self, why="the page asked for it"):
        """Delete the sandbox. This is the promise the whole design rests on."""
        if not os.path.isdir(self.root):
            receipt = {"removed": 0, "path": self.root, "already": True, "why": why}
            with self.lock:
                self.last_cleanup = receipt
            return receipt
        if self.cfg.keep:
            return {"removed": 0, "path": self.root, "kept": True, "why": why,
                    "note": "--keep was given, nothing deleted"}
        size = 0
        files = 0
        leftovers = []
        for base, _dirs, names in os.walk(self.root):
            for n in names:
                full = os.path.join(base, n)
                try:
                    if os.path.islink(full):
                        continue          # borrowed from the cache: not ours to count
                    size += os.path.getsize(full)
                    files += 1
                except OSError:
                    pass
        self._stop_tunnel()
        shutil.rmtree(self.root, ignore_errors=True)
        with self.lock:
            self.cleanup_deadline = None
        # Anything of ours that a crash could have scattered outside the sandbox.
        for pattern in ("webmc-curl-*.conf", "webmc-*.part"):
            import glob as _glob
            for stray in _glob.glob(os.path.join(os.environ.get("TMPDIR", "/tmp"), pattern)):
                try:
                    size += os.path.getsize(stray)
                    os.unlink(stray)
                    leftovers.append(stray)
                except OSError:
                    pass
        gone = not os.path.exists(self.root)
        receipt = {"removed": size, "files": files, "path": self.root, "gone": gone,
                   "why": why, "strays": leftovers, "at": time.time()}
        with self.lock:
            self.last_cleanup = receipt
        self.log("deleted %s (%s files, %.1f MB freed)%s"
                 % (self.root, files, size / 1048576.0,
                    "" if gone else " -- SOMETHING IS STILL THERE"))
        return receipt

    def restore(self, blob):
        """Put a previous session's bundle back into the game dir before launch.

        The page calls this *after* cleanup has deleted the sandbox, so the whole
        tree has to be rebuilt first - on a warm cache that costs nothing.
        """
        self._prepare_root()
        tmp = os.path.join(self.root, "restore-%s.zip" % secrets.token_hex(4))
        with open(tmp, "wb") as fh:
            fh.write(blob)
        count = 0
        with zipfile.ZipFile(tmp) as zf:
            for info in zf.namelist():
                if info.startswith("/") or ".." in info.split("/"):
                    continue
                zf.extract(info, self.game_dir)
                count += 1
        os.unlink(tmp)
        self.log("restored %d files from the browser into the sandbox" % count)
        return {"restored": count}

    # ---------------------------------------------------------------- pushed mods
    @property
    def local_mods_dir(self):
        """Where the jars the visitor handed over live. Shared by every session, because a mod
        list belongs to the visitor rather than to one launch."""
        if self.manager:
            return self.manager.mods_dir
        return os.path.join(self.root, "localmods")

    def _pending_mod_paths(self):
        directory = self.local_mods_dir
        if not os.path.isdir(directory):
            return []
        return [os.path.join(directory, n) for n in sorted(os.listdir(directory))
                if n.endswith(".jar")]

    def accept_mod(self, name, blob):
        """Take a jar from the browser.

        It lives inside the sandbox and is deleted with it; the page keeps its own
        copy, so a visitor's mods travel with the browser rather than with the
        computer - which is the same promise the rest of this makes.
        """
        safe = re.sub(r"[^A-Za-z0-9._+ -]", "_", os.path.basename(name or "mod.jar"))
        if not safe.lower().endswith(".jar"):
            safe += ".jar"
        if not blob:
            raise ValueError("empty file")
        if len(blob) > 64 * 1024 * 1024:
            raise ValueError("that jar is larger than 64 MB")
        if blob[:2] != b"PK":
            raise ValueError("that is not a jar (a jar is a zip, and this does not start like one)")
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                names = zf.namelist()
                is_mod = any(n == "fabric.mod.json" or n.endswith("/fabric.mod.json")
                             or n == "META-INF/mods.toml" or n == "mcmod.info" for n in names)
                title = version = None
                if "fabric.mod.json" in names:
                    meta = json.loads(zf.read("fabric.mod.json").decode("utf-8", "replace"))
                    title = meta.get("name") or meta.get("id")
                    version = meta.get("version")
        except (zipfile.BadZipFile, ValueError, KeyError):
            raise ValueError("that jar could not be read")
        if not is_mod:
            raise ValueError("no fabric.mod.json, META-INF/mods.toml or mcmod.info inside, "
                             "so this is a library or a plain zip rather than a mod")
        os.makedirs(self.local_mods_dir, exist_ok=True)
        dest = os.path.join(self.local_mods_dir, safe)
        with open(dest, "wb") as fh:
            fh.write(blob)
        entry = {"name": safe, "bytes": len(blob), "title": title, "version": version}
        with self.lock:
            self.pending_mods = [m for m in self.pending_mods if m["name"] != safe] + [entry]
        self.log("mod %s taken from the browser (%s, %.1f MB)%s"
                 % (safe, title or "unnamed", len(blob) / 1048576.0,
                    "" if version is None else " " + str(version)))
        return entry

    def drop_mod(self, name):
        safe = re.sub(r"[^A-Za-z0-9._+ -]", "_", os.path.basename(name or ""))
        path = os.path.join(self.local_mods_dir, safe)
        if os.path.exists(path):
            os.unlink(path)
        with self.lock:
            self.pending_mods = [m for m in self.pending_mods if m["name"] != safe]
        return {"dropped": safe}

    def clear_mods(self):
        dropped = 0
        if os.path.isdir(self.local_mods_dir):
            for n in os.listdir(self.local_mods_dir):
                try:
                    os.unlink(os.path.join(self.local_mods_dir, n))
                    dropped += 1
                except OSError:
                    pass
        with self.lock:
            self.pending_mods = []
        return {"dropped": dropped}

    def search_mods(self, query, mc_version=None, limit=12):
        """Modrinth search, done here rather than in the page: the helper can reach it
        through the visitor's proxy on a network where the browser cannot."""
        q = urllib.parse.urlencode({
            "query": query, "limit": limit,
            "facets": json.dumps([["project_type:mod"], ["categories:fabric"]]
                                 + ([["versions:%s" % mc_version]] if mc_version else [])),
        })
        data = self.curl.json("%s/search?%s" % (MODRINTH_API, q), headers=self._ua())
        out = []
        for hit in data.get("hits", []):
            out.append({
                "slug": hit.get("slug"), "title": hit.get("title"),
                "description": (hit.get("description") or "")[:200],
                "downloads": hit.get("downloads"), "author": hit.get("author"),
                "icon": hit.get("icon_url"), "versions": (hit.get("versions") or [])[-4:],
            })
        return {"results": out, "query": query}

    # ---------------------------------------------------------------- helpers
    def _ua(self):
        return ("User-Agent: %s/%s (+https://github.com/derek/webmc)" % (NAME, VERSION),)

    def _progress_cb(self, label):
        def cb(done, total, _round=None):
            self._progress(label, done, total)
        return cb


def materialise(args, sub, features):
    """Flatten and substitute Mojang's argument list."""
    out = []
    for a in args:
        if isinstance(a, str):
            out.append(a)
            continue
        if not rules_allow(a.get("rules"), features):
            continue
        val = a.get("value")
        if isinstance(val, list):
            out.extend(val)
        elif val is not None:
            out.append(val)
    return [substitute(x, sub) for x in out]


PLACEHOLDER = re.compile(r"\$\{([a-zA-Z0-9_]+)\}")


def substitute(text, sub):
    return PLACEHOLDER.sub(lambda m: str(sub.get(m.group(1), "")), text)


def offline_uuid_for(name):
    digest = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuidlib.UUID(bytes=bytes(digest)))


def shell_quote(s):
    if re.match(r"^[A-Za-z0-9_@%+=:,./-]+$", s or ""):
        return s
    return "'" + (s or "").replace("'", "'\\''") + "'"


def port_open(host, port, timeout=0.25):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def detect_proxy(explicit, curl_log):
    if explicit in ("none", "direct", ""):
        return None
    if explicit:
        return explicit
    for cand in PROXY_CANDIDATES:
        host_port = cand.split("://", 1)[1]
        host, port = host_port.split(":")
        if not port_open(host, int(port)):
            continue
        try:
            probe = Curl(proxy=cand)
            probe.get(MANIFEST_URL, dest=None, timeout=25)
            curl_log("proxy: %s (Mojang reachable through it)" % cand)
            return cand
        except CurlError as exc:
            curl_log("proxy candidate %s could not reach Mojang: %s" % (cand, exc))
    return None


# --------------------------------------------------------------------------- page

PAGE_SUFFIX = """
<script>
window.__WEBMC__ = { helper: "http://127.0.0.1:%(port)d", token: "%(token)s",
                     served: "helper" };
</script>
"""


# --------------------------------------------------------------------------- http

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "%s/%s" % (NAME, VERSION)
    protocol_version = "HTTP/1.1"

    # ---- plumbing
    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("http %s\n" % (fmt % args))

    def _cors(self):
        origin = self.headers.get("Origin")
        allowed = None
        if origin:
            if origin in self.server.extra_origins or any(p.match(origin) for p in ORIGIN_PATTERNS):
                allowed = origin
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "content-type, x-webmc-token, access-control-request-private-network")
        # Chrome 142+ gates public -> loopback behind Local Network Access. Answering
        # the preflight with this header is what keeps the bridge usable where an
        # admin has not blocked it outright.
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cache-Control", "no-store")

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, indent=1))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return b""
        return self.rfile.read(n)

    def _json_body(self):
        raw = self._body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def _authed(self):
        if self.path.split("?")[0] in ("/api/hello", "/", "/go", "/favicon.ico"):
            return True
        return self.headers.get("X-WebMC-Token") == self.server.token

    def _guard(self):
        if self._authed():
            return True
        self._json(403, {"error": "bad or missing X-WebMC-Token"})
        return False

    # ---- routes
    def do_OPTIONS(self):
        self._send(204)

    def do_HEAD(self):
        self.do_GET()

    def _session(self, query):
        """The session a request is about: ?id=… , or the newest one still working."""
        sid = (query.get("id") or [None])[0]
        return self.server.registry.pick(sid)

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        reg = self.server.registry
        s = self._session(query)
        if s:
            s.touch()
        else:
            reg.touch()
        if path == "/favicon.ico":
            return self._send(204)
        if path == "/":
            return self._page()
        if path == "/go":
            return self._go(query)
        if path == "/api/hello":
            return self._json(200, {
                "ok": True, "name": NAME, "version": VERSION,
                "platform": {"os": os_name(), "arch": os_arch(), "version": os_version()},
                "sandbox": (s.root if s else reg.cfg.root),
                "sessions": len(reg.sessions),
                "store": {"dir": reg.store,
                          "kept": len([n for n in os.listdir(reg.store) if n.endswith(".zip")])
                                  if os.path.isdir(reg.store) else 0},
                "proxy": self.server.curl.proxy or "direct",
                "relay": (s.cfg.relay if s else reg.cfg.relay) or None,
                "relayBuiltIn": len(DEFAULT_RELAYS),
                "relayMode": ("custom" if (s.cfg.relay if s else reg.cfg.relay)
                              and (s.cfg.relay if s else reg.cfg.relay) not in DEFAULT_RELAYS
                              else "auto" if (s.cfg.relay if s else reg.cfg.relay) else "off"),
                "downloads": self.server.curl.bytes_in,
                "autocleanSeconds": (s.cfg.autoclean if s else reg.cfg.autoclean),
                "footprint": machine_footprint(cache=s.cfg.cache if s else reg.cfg.cache),
                "features": ["fabric", "real-jvm", "opfs-bundle", "restore", "modrinth",
                             "local-mods", "mod-search", "audit"],
            })
        if not self._guard():
            return
        if path == "/api/sessions":
            return self._json(200, {"sessions": reg.list()})
        if path == "/api/status":
            if not s:
                return self._json(200, {"state": "idle", "phase": "idle", "phaseLabel": "Ready",
                                        "progress": {"done": 0, "total": 0, "percent": None},
                                        "instance": {}, "pid": None, "exitCode": None,
                                        "result": None})
            return self._json(200, s.status())
        if path == "/api/search":
            q = (query.get("q") or [""])[0].strip()
            if not q:
                return self._json(400, {"error": "give me something to search for (?q=sodium)"})
            try:
                return self._json(200, s.search_mods(q, (query.get("mc") or [None])[0]))
            except CurlError as exc:
                return self._json(502, {"error": str(exc)})
        if path == "/api/localmods":
            return self._json(200, {"mods": [{"name": m["name"], "bytes": m["bytes"],
                                              "title": m.get("title"),
                                              "version": m.get("version")}
                                             for m in reg.mods],
                                    "dir": reg.mods_dir})
        if path == "/api/versions":
            try:
                return self._json(200, s.versions())
            except CurlError as exc:
                return self._json(502, {"error": str(exc)})
        if path == "/api/log":
            since = int((query.get("since") or ["0"])[0] or 0)
            if not s:
                return self._json(200, {"lines": [], "next": since})
            lines, nxt = s.lines_since(since)
            return self._json(200, {"lines": lines, "next": nxt})
        if path == "/api/result":
            if not s:
                return self._json(404, {"error": "no session"})
            return self._json(200, {"result": s.result, "state": s.state,
                                    "exitCode": s.exit_code, "error": s.error})
        if path == "/api/bundle.zip":
            if not s or not s.result or not os.path.exists(s.bundle_path):
                return self._json(404, {"error": "no bundle yet"})
            with open(s.bundle_path, "rb") as fh:
                blob = fh.read()
            return self._send(200, blob, "application/zip", {
                "Content-Disposition": 'attachment; filename="webmc-session.zip"'})
        return self._json(404, {"error": "no such endpoint", "path": path})

    def do_POST(self):
        parts = urllib.parse.urlsplit(self.path)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        reg = self.server.registry
        s = self._session(query)
        if s:
            s.touch()
        else:
            reg.touch()
        if not self._guard():
            return
        # ---- the mods a visitor brings with them
        if path == "/api/localmods":
            if (query.get("clear") or [""])[0]:
                return self._json(200, reg.clear_mods())
            drop = (query.get("drop") or [""])[0]
            if drop:
                return self._json(200, reg.drop_mod(drop))
            name = self.headers.get("X-WebMC-Mod-Name") or "mod.jar"
            try:
                return self._json(200, reg.accept_mod(name, self._body()))
            except ValueError as exc:
                return self._json(400, {"error": str(exc), "name": name})
        if path == "/api/launch":
            body = self._json_body()
            opts = {
                "version": body.get("version") or DEFAULT_VERSION,
                "loader": body.get("loader") or "auto",
                "loaderKind": body.get("loaderKind") or "fabric",
                "relay": body.get("relay") or self.server.registry.cfg.relay,
                "tunnelServer": body.get("tunnelServer"),
                "tunnelPort": body.get("tunnelPort"),
                "tunnelEverything": bool(body.get("tunnelEverything")),
                "restricted": bool(body.get("restricted")),
                "mods": body.get("mods") or [],
                "username": body.get("username") or "Player",
                "memory": body.get("memory") or DEFAULT_MEMORY,
                "assets": body.get("assets", True),
            }
            try:
                # Every launch is its own session, so a second window - or another version - can
                # run while the first is still playing.
                started = reg.launch(opts)
            except Exception as exc:  # noqa: BLE001
                return self._json(400, {"error": str(exc)})
            return self._json(202, {"started": True, "id": started.sid, "instance": opts})
        if path == "/api/stop":
            # One button, whichever state a session is in: mid-download it cancels, running it
            # kills the game. With no session named it stops the newest one that is working.
            if not s:
                return self._json(200, {"cancelled": False, "note": "nothing is running"})
            return self._json(200, s.cancel())
        if path == "/api/restore":
            blob = self._body()
            if not s:
                # The page restores before it launches, so keep the bundle until then: the next
                # launch unpacks it into its own sandbox before the game starts.
                reg.pending_restore = blob
                return self._json(200, {"queued": len(blob)})
            if not blob:
                return self._json(400, {"error": "empty body"})
            try:
                return self._json(200, s.restore(blob))
            except Exception as exc:  # noqa: BLE001
                return self._json(400, {"error": "not a usable bundle: %s" % exc})
        if path == "/api/purge":
            body = self._read_json()
            result = purge(cache=self.server.registry.cfg.cache, log=lambda m: print(m, flush=True))
            return self._json(200, {"ok": True, "removed": result["removed"],
                                    "bytes": result["bytes"]})
        if path == "/api/cleanup":
            if not s:
                return self._json(200, {"removed": 0, "gone": True, "already": True})
            return self._json(200, s.cleanup())
        return self._json(404, {"error": "no such endpoint", "path": path})

    # ---- the helper's own copy of the page
    def _page(self):
        page = self.server.page_path
        if (not page or not os.path.exists(page)) and _EMBEDDED_PAGE:
            # Self-healing: the sandbox (and the page written into it) is deleted when a session
            # ends, and the page has to come back for the next visit.
            page = materialise_embedded_page(os.environ.get("WEBMC_CLEANUP_DIR")
                                             or os.path.dirname(self.server.registry.cfg.root)) or page
            self.server.page_path = page
        if not page or not os.path.exists(page):
            return self._json(200, {
                "name": NAME, "version": VERSION,
                "note": "page not found next to the helper; use /api/* or pass --page",
                "hint": "http://127.0.0.1:%d/api/hello" % self.server.server_address[1],
            })
        with open(page, "rb") as fh:
            html = fh.read().decode("utf-8")
        inject = PAGE_SUFFIX % {"port": self.server.server_address[1],
                                "token": self.server.token}
        # This has to land before the page's own script runs, or the page cannot see the
        # token it was just handed. Injecting it at the end made a helper-served page
        # say "not connected" while talking to the helper that served it.
        placed = False
        for marker, before in (("<head>", False), ("</head>", True), ("</body>", True)):
            if marker in html:
                html = html.replace(marker, (inject + marker) if before else (marker + inject), 1)
                placed = True
                break
        if not placed:
            html = inject + html
        self._send(200, html, "text/html; charset=utf-8")

    def _go(self, query):
        """Hand the token to a hosted copy of the page, via the URL fragment."""
        target = (query.get("to") or [""])[0]
        if not target:
            return self._send(400, "missing ?to=", "text/plain")
        parsed = urllib.parse.urlsplit(target)
        ok = (parsed.scheme in ("http", "https") and (
            any(p.match("%s://%s" % (parsed.scheme, parsed.netloc)) for p in ORIGIN_PATTERNS)
            or parsed.netloc.endswith("github.io")))
        if not ok:
            return self._send(400, "refusing to redirect to %s" % target, "text/plain")
        frag = "helper=http://127.0.0.1:%d&t=%s" % (self.server.server_address[1],
                                                    self.server.token)
        sep = "&" if "#" in target else "#"
        loc = target + sep + frag
        self.send_response(302)
        self._cors()
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, registry, curl, token, page_path, origins, verbose):
        self.registry = registry
        self.curl = curl
        self.token = token
        self.page_path = page_path
        self.extra_origins = set(origins)
        self.verbose = verbose
        super().__init__(addr, handler)


# --------------------------------------------------------------------------- cli

def default_page(root):
    """Where the launcher page is, when it is not given.

    One file has to be able to serve its own page, so an embedded copy is written out next to the
    sandbox's files. That copy has to be re-creatable, not written once: the sandbox is deleted
    when a session ends, and the page used to be deleted with it, so after the first session the
    helper answered "/" with "page not found next to the helper".
    """
    if _EMBEDDED_PAGE:
        return materialise_embedded_page(root)
    here = globals().get("__file__")
    if not here:
        return None
    guess = os.path.join(os.path.dirname(os.path.abspath(here)), os.pardir, "page", "index.html")
    return os.path.abspath(guess) if os.path.exists(guess) else None


def materialise_embedded_page(root):
    import base64
    path = os.path.join(root, "page.html")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        os.makedirs(root, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(_EMBEDDED_PAGE))
        return path
    except Exception:
        return None


def default_root():
    return os.path.join("/tmp", "webmc-%s" % secrets.token_hex(4))


def sweep_stale_sandboxes(older_than=6 * 3600, keep=None, log=print):
    """A helper that was killed hard (or a machine that went down) leaves its
    sandbox behind. /tmp is emptied on reboot anyway, but the promise is that
    nothing survives a session, so clear out anything old enough that it cannot
    belong to a session still in progress."""
    removed = 0
    freed = 0
    now = time.time()
    try:
        names = os.listdir("/tmp")
    except OSError:
        return 0, 0
    for name in names:
        if not name.startswith("webmc-"):
            continue
        path = os.path.join("/tmp", name)
        if keep and os.path.abspath(path) == os.path.abspath(keep):
            continue
        if not os.path.isdir(path):
            continue
        # Ours means: the default naming (webmc-<8 hex>) or a directory that carries
        # our owner.json (which also covers a custom --root). A directory someone else
        # named webmc-something is not ours to delete.
        owner = read_owner(path)
        if not re.fullmatch(r"webmc-[0-9a-f]{8}", name) and owner is None:
            continue
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        # Ours with no owner file can only mean a hard kill in the first second of a session, before
        # the note was written. Ours by name, so a quarter of an hour is enough to be sure it is not
        # somebody's session that is simply very quiet.
        abandoned = owner is not None and not pid_alive(owner.get("pid"))
        if owner is None and re.fullmatch(r"webmc-[0-9a-f]{8}", name) and age > 900:
            abandoned = True
        # Two reasons to delete: the helper that owned it is gone (so nobody will ever
        # clean it), or it is simply old. The first one is why this is fast: a sandbox
        # left by a crash is removed on the next run, not six hours later.
        if not abandoned and age < older_than:
            continue
        try:
            for base, _dirs, files in os.walk(path):
                for f in files:
                    full = os.path.join(base, f)
                    try:
                        if not os.path.islink(full):
                            freed += os.path.getsize(full)
                    except OSError:
                        pass
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
            log("cleared leftover sandbox %s (%s)"
                % (path, "its helper is gone" if abandoned else "older than %.0fh" % (older_than / 3600.0)))
        except OSError:
            pass
    import glob as _glob
    for stray in _glob.glob(os.path.join(os.environ.get("TMPDIR", "/tmp"), "webmc-curl-*.conf")):
        try:
            freed += os.path.getsize(stray)
            os.unlink(stray)
            removed += 1
        except OSError:
            pass
    if removed:
        log("cleared %d leftover item(s) from earlier runs (%.0f MB)"
            % (removed, freed / 1048576.0))
    return removed, freed


def read_owner(path):
    try:
        with open(os.path.join(path, "owner.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    """Is that pid still a live process of ours? Unknown counts as alive, so the
    sweep never deletes a sandbox it cannot be sure about."""
    if not pid:
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, OSError):
        return True


def _boot_id():
    """Distinguishes 'this pid is gone' from 'this pid was reused after a reboot'."""
    try:
        return "%.0f" % time.time()
    except Exception:
        return ""


AGENT_LABEL = "dev.amphibian.webmc-helper"
INSTALLED_DIR = "~/Library/Application Support/webmc"
AGENT_PLIST = "~/Library/LaunchAgents/dev.amphibian.webmc-helper.plist"
LOG_FILE = "~/Library/Logs/webmc-helper.log"
SHIPPED = (("helper/webmc_helper.py", 0o755),
           ("helper/start-helper.command", 0o755),
           ("page/index.html", 0o644))


def session_store_dir(cfg=None):
    """Where a finished session is kept so the next launch can start from it.

    Your world has to live somewhere between sessions, and the browser is the wrong only place for
    it: storage belongs to an origin, and this launcher legitimately runs on two of them (the helper's
    own page at 127.0.0.1:7645, and a hosted copy on github.io). A session saved by one is invisible
    to the other, which is exactly how worlds and options come to reset. So the helper keeps a copy
    of each session's bundle as well, and restores from it when the page has nothing to offer.
    """
    if cfg is not None and getattr(cfg, "store", None):
        return os.path.abspath(os.path.expanduser(cfg.store))
    if os_name() == "osx":
        base = "~/Library/Application Support/webmc"
    elif os_name() == "windows":
        base = os.path.join(os.environ.get("LOCALAPPDATA") or "~", "webmc")
    else:
        base = os.path.join(os.environ.get("XDG_DATA_HOME") or "~/.local/share", "webmc")
    return os.path.expanduser(os.path.join(base, "sessions"))


def installed_paths():
    return {"dir": os.path.expanduser(INSTALLED_DIR),
            "plist": os.path.expanduser(AGENT_PLIST),
            "log": os.path.expanduser(LOG_FILE)}


def agent_plist_text(python, helper, page_url, port):
    def esc(text):
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    args = [python, helper, "--server-only", "--port", str(port)]
    if page_url:
        args += ["--page-url", page_url]
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array>%s</array>
  <key>RunAtLoad</key><true/>
  <!-- Come back if it ever stops: whether it crashed or exited on its own after a long quiet spell,
       launchd starts it again in seconds and it deletes its sandbox on the way out, so nothing
       accumulates. This is what makes "installed" mean "always there" instead of "was there once". -->
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>WorkingDirectory</key><string>%s</string>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
</dict>
</plist>
""" % (AGENT_LABEL,
       "".join("<string>%s</string>" % esc(a) for a in args),
       esc(os.path.dirname(helper)),
       esc(os.path.expanduser(LOG_FILE)), esc(os.path.expanduser(LOG_FILE)))


def install(source_py, page_url=None, port=7645, log=print, open_page=True):
    """Install for a Mac with no terminal in the way.

    macOS runs a user's own launch agents without an administrator, so this needs no
    privilege: it copies the helper into the user's Application Support, writes one plist
    into the user's LaunchAgents, and asks launchd to start it now and at every login. The
    point is that nobody has to open Terminal or keep a window alive - the page finds a
    helper already running.

    It still keeps nothing of the game: the sandbox under /tmp is deleted as usual, and
    `--uninstall` removes both files in one step.
    """
    if os_name() != "osx":
        log("  --install is the macOS path; elsewhere run it however you start things")
        return 1
    paths = installed_paths()
    # Already up (a second double-click, or an opened file again): do not start a second one on
    # the same port and report it as a failure.
    if already_running("127.0.0.1", port):
        log("  a helper is already running on 127.0.0.1:%d - nothing to do" % port)
        if open_page:
            url = "http://127.0.0.1:%d/" % port
            if page_url:
                url = "%sgo?to=%s" % (url, urllib.parse.quote(page_url, safe=""))
            threading.Timer(0.8, lambda: subprocess.run(["open", url])).start()
        return 0
    os.makedirs(os.path.join(paths["dir"], "helper"), exist_ok=True)
    if not source_py or not os.path.exists(source_py):
        log("  cannot find the helper to install (%s)" % source_py)
        return 2
    import re as _re
    text = open(source_py, "rb").read()
    if b"_EMBEDDED_PAGE = \"\"" in text:
        # A copy from the repository cannot serve its own page; the single-file build can, because
        # the page is inside it. Installing the repository copy is still fine (it keeps whatever
        # page sits next to it), so only the missing-page case is worth a word.
        log("  note: this copy has no page inside it; install the downloadable file instead if "
            "you want the helper to serve its own page")
    helper = os.path.join(paths["dir"], "helper", "webmc_helper.py")
    with open(helper, "wb") as fh:
        fh.write(text)
    os.chmod(helper, 0o755)
    log("  installed the helper to %s" % helper)
    plist = agent_plist_text(sys.executable or "/usr/bin/python3", helper,
                             page_url, port)
    os.makedirs(os.path.dirname(paths["plist"]), exist_ok=True)
    with open(paths["plist"], "w") as fh:
        fh.write(plist)
    log("  wrote a launch agent: %s" % paths["plist"])

    uid = os.getuid()
    target = "gui/%d" % uid
    subprocess.run(["launchctl", "bootout", "%s/%s" % (target, AGENT_LABEL)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = subprocess.run(["launchctl", "bootstrap", target, paths["plist"]],
                             capture_output=True, text=True)
    if started.returncode != 0:
        log("  launchctl could not start it: %s" % (started.stderr or started.stdout).strip())
        log("  the page still works if you start it by hand; see --help")
    else:
        # bootstrap already launches it (RunAtLoad). Asking again with kickstart -k kills the
        # instance that just came up - which is exactly the "installed but nothing is
        # listening" symptom this had before.
        for _ in range(20):
            if already_running("127.0.0.1", port):
                break
            time.sleep(0.5)
        if not already_running("127.0.0.1", port):
            subprocess.run(["launchctl", "kickstart", "%s/%s" % (target, AGENT_LABEL)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.5)
        if already_running("127.0.0.1", port):
            log("  launchd started it, and starts it again at every login")
        else:
            log("  launchd has it, but nothing is answering yet - the page will say so; "
                "see %s" % paths["log"])
    log("  log: %s" % paths["log"])
    log("  remove it again with: python3 '%s' --uninstall" % helper)

    if open_page:
        url = "http://127.0.0.1:%d/" % port
        if page_url:
            url = "%sgo?to=%s" % (url, urllib.parse.quote(page_url, safe=""))
        threading.Timer(1.2, lambda: subprocess.run(["open", url])).start()
    return 0


def dir_bytes(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def temp_sandboxes():
    """Every sandbox this helper left in the temp directory, newest last."""
    import glob as _glob
    found, seen = [], set()
    for base in {os.environ.get("TMPDIR", "/tmp"), "/tmp"}:
        for path in _glob.glob(os.path.join(base, "webmc-*")):
            name = os.path.basename(path)
            if path in seen:
                continue
            if not (os.path.isdir(path) and (read_owner(path) is not None
                                             or re.fullmatch(r"webmc-[0-9a-f]{8}", name))):
                continue
            seen.add(path)
            found.append(path)
    return found


def machine_footprint(cache=None):
    """Everything of ours on this machine, with sizes, so "nothing is left" is checkable.

    The audit used to look only at /tmp. That is how 592 MB of cached assets sat on a machine whose
    owner had asked for nothing to be left there: the promise has to be an inventory, not a glance.
    """
    paths = installed_paths()
    items = []
    for key, label in (("dir", "installed helper"), ("plist", "login agent"),
                       ("log", "log file")):
        path = os.path.expanduser(paths[key])
        if os.path.exists(path) or os.path.isdir(path):
            items.append({"what": label, "path": path,
                          "bytes": dir_bytes(path) if os.path.isdir(path) else os.path.getsize(path)})
    if cache:
        cache_path = os.path.abspath(os.path.expanduser(cache))
        if os.path.isdir(cache_path):
            items.append({"what": "download cache", "path": cache_path,
                          "bytes": dir_bytes(cache_path)})
    store = session_store_dir()
    if os.path.isdir(store):
        items.append({"what": "kept sessions", "path": store, "bytes": dir_bytes(store)})
    for path in temp_sandboxes():
        items.append({"what": "sandbox", "path": path, "bytes": dir_bytes(path)})
    return items


def purge(cache=None, log=print, keep_log=False):
    """Remove every trace of this launcher from this machine, and say what was removed."""
    items = machine_footprint(cache=cache)
    if not items:
        log("  this machine has nothing of ours on it: no files, no cache, no login item")
        return {"removed": [], "bytes": 0}
    freed = 0
    removed = []
    # the login item first, so nothing restarts while the rest goes
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, AGENT_LABEL)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for item in items:
        if keep_log and item["what"] == "log file":
            continue
        path = item["path"]
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
            removed.append(item["what"])
            freed += item["bytes"]
            log("  removed %-16s %s" % (item["what"], path))
        except Exception as exc:  # noqa: BLE001
            log("  could not remove %s: %s" % (path, exc))
    log("  %d thing(s), %.1f MB. Nothing of ours is on this machine now."
        % (len(removed), freed / 1048576.0))
    return {"removed": removed, "bytes": freed}


def uninstall(log=print):
    paths = installed_paths()
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, AGENT_LABEL)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    removed = []
    for key in ("plist", "dir"):
        path = paths[key]
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)
        elif os.path.exists(path):
            os.unlink(path)
            removed.append(path)
    cleared, freed = sweep_stale_sandboxes(older_than=0, keep=None, log=lambda *_: None)
    log("  removed: %s" % (", ".join(removed) if removed else "nothing (it was not installed)"))
    if cleared:
        # sweep_stale_sandboxes returns (how many, how many bytes). Getting that order wrong once
        # printed five million "items" for 5.5 MB of sandboxes, which is a good way to frighten
        # somebody who is about to read the output of an uninstaller.
        log("  and cleared %d leftover sandbox(es), %.1f MB" % (cleared, freed / 1048576.0))
    if os.path.exists(os.path.expanduser(paths["log"])):
        try:
            os.unlink(os.path.expanduser(paths["log"]))
            log("  removed the log too: %s" % paths["log"])
        except OSError:
            pass
    return 0


def audit(root=None, cache=None, clean=False, log=print):
    """Say what *this helper* has on the machine, and optionally remove it.

    Written for the question the whole design is about: "did it leave anything
    behind?" Only things this helper creates are claimed here - a sandbox
    (webmc-<8 hex>), or a stray curl config. Other webmc-* entries in the temp
    directory belong to other parts of the project and are reported as a count, never
    touched.
    """
    import glob as _glob
    tmp = os.environ.get("TMPDIR", "/tmp")
    mine = []
    theirs = 0

    def size_of(path):
        total = 0
        if os.path.isdir(path):
            for base, _dirs, files in os.walk(path):
                for f in files:
                    full = os.path.join(base, f)
                    try:
                        if not os.path.islink(full):
                            total += os.path.getsize(full)
                    except OSError:
                        pass
        else:
            try:
                total = os.path.getsize(path)
            except OSError:
                pass
        return total

    seen = set()
    for pattern in ("webmc-*",):
        for base in (tmp, "/tmp"):
            for path in _glob.glob(os.path.join(base, pattern)):
                if path in seen:
                    continue
                is_dir = os.path.isdir(path)
                owner = read_owner(path) if is_dir else None
                ours = (is_dir and (owner is not None
                                    or re.fullmatch(r"webmc-[0-9a-f]{8}", os.path.basename(path)))) \
                    or (not is_dir and re.match(r"webmc-(curl-.*\.conf|.*\.part)$",
                                                os.path.basename(path)))
                if not ours:
                    continue
                seen.add(path)
                mine.append({"path": path,
                             "kind": "sandbox" if is_dir else "stray file",
                             "bytes": size_of(path), "owner": owner,
                             "abandoned": bool(owner and not pid_alive(owner.get("pid")))})
    for base in (tmp, "/tmp"):
        for path in _glob.glob(os.path.join(base, "webmc-*")):
            if path in seen:
                continue
            if cache and path == os.path.abspath(os.path.expanduser(cache)):
                continue
            theirs += 1
    if root and os.path.isdir(root) and os.path.abspath(root) not in seen:
        mine.append({"path": root, "kind": "this session's sandbox", "bytes": size_of(root),
                     "owner": read_owner(root), "abandoned": False})

    log("")
    log("  %s %s - what is on this machine" % (NAME, VERSION))
    log("  " + "-" * 58)
    if not mine:
        log("  nothing: no sandbox, no stray file. /tmp has none of ours.")
    for item in mine:
        log("  %-20s %s" % (item["kind"], item["path"]))
        if item["owner"]:
            log("  %-20s started %s by pid %s%s"
                % ("", time.strftime("%H:%M:%S", time.localtime(item["owner"].get("started", 0))),
                   item["owner"].get("pid"),
                   " - that process is gone, so this is litter" if item["abandoned"]
                   else " - running now"))
        log("  %-20s %.1f MB" % ("", item["bytes"] / 1048576.0))
    store = session_store_dir()
    if os.path.isdir(store):
        kept = sorted(n for n in os.listdir(store) if n.endswith(".zip"))
        log("  %-20s %s" % ("kept sessions", store))
        log("  %-20s %d session(s), %.1f MB - your worlds and options, so the next launch does not "
            % ("", len(kept), size_of(store) / 1048576.0))
        log("  %-20s start empty (--forget-sessions removes them)" % "")
    if cache:
        path = os.path.abspath(os.path.expanduser(cache))
        size = size_of(path) if os.path.isdir(path) else 0
        log("  %-20s %s" % ("cache", path))
        log("  %-20s %.1f MB %s" % ("", size / 1048576.0,
              "- kept on purpose, only ever used with --cache" if size else "- not present"))
    paths = installed_paths()
    if os.path.exists(paths["plist"]) or os.path.isdir(paths["dir"]):
        log("  %-20s %s" % ("installed", paths["dir"]))
        log("  %-20s %s" % ("", paths["plist"]))
        log("  %-20s installed on purpose so the page always finds a helper; remove both"
            % "")
        log("  %-20s with --uninstall (no administrator needed)" % "")
    if theirs:
        log("  %-20s %d other webmc-* entries in the temp directory that this helper"
            % ("not ours", theirs))
        log("  %-20s did not create (the test suite's webmc-site-* and friends)." % "")
    log("  %-20s %s" % ("writes to", (root or "/tmp/webmc-<random>/") +
                                        " - and nothing else; that path is deleted when "
                                        "the session ends"))
    log("  %-20s %s" % ("privileges", "none: no sudo, no administrator, no installer, no write "
                                      "outside /tmp and your own home; it listens on "
                                      "127.0.0.1 only"))
    log("")
    if clean:
        removed = 0
        freed = 0
        for item in mine:
            if item["kind"] == "this session's sandbox":
                continue
            try:
                if os.path.isdir(item["path"]):
                    shutil.rmtree(item["path"], ignore_errors=True)
                else:
                    os.unlink(item["path"])
                removed += 1
                freed += item["bytes"]
            except OSError:
                pass
        log("  removed %d of ours (%.1f MB); nothing else was touched"
            % (removed, freed / 1048576.0))
        log("")
    return mine


def install_signal_handlers(log=print):
    """Closing the terminal window, or any ordinary kill, has to run the cleanup
    that deletes the sandbox. Without this a double-clicked .command that the
    visitor closes would leave 600 MB under /tmp."""
    def bye(signum, _frame):
        try:
            name = signal.Signals(signum).name
        except Exception:
            name = str(signum)
        log("\n  received %s - cleaning up" % name)
        raise SystemExit(0)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        try:
            signal.signal(sig, bye)
        except (ValueError, OSError, AttributeError):
            pass


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog=NAME,
        description="Run real Minecraft + Fabric locally from a web page, keep nothing.")
    ap.add_argument("--port", type=int, default=7645)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--root", default=None,
                    help="sandbox directory (default /tmp/webmc-<random>)")
    ap.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                    help="extra page origin allowed to talk to this helper")
    ap.add_argument("--page", default=None, help="path to the launcher page to serve at /")
    ap.add_argument("--page-url", default=None,
                    help="hosted page to hand the token to when /go is used")
    ap.add_argument("--open", action="store_true", help="open the browser at / (or /go)")
    ap.add_argument("--java", default=None, help="java executable to use")
    ap.add_argument("--proxy", default=None,
                    help="SOCKS/HTTP proxy for downloads, or 'none' (default: auto)")
    ap.add_argument("--game-proxy", default=os.environ.get("WEBMC_GAME_PROXY") or None, metavar="URL",
                    help="how the game's own connections leave this machine: a SOCKS5 URL like "
                         "socks5h://127.0.0.1:1080. Defaults to --proxy, then to a proxy found on the "
                         "usual ports, then to a direct connection")
    ap.add_argument("--proxy-game", action="store_true",
                    help="also send the game's own connections through the relay: a local SOCKS5"
                         " proxy, with the JVM pointed at it, so servers, sign-in and skins work"
                         " on a network that blocks everything but HTTPS")
    ap.add_argument("--restricted", action="store_true",
                    help="restricted mode: use only the Java runtime in the sandbox, never a"
                         " system one, and never look outside /tmp (for machines where Java is"
                         " present but blocked, or policies watch what runs)")
    ap.add_argument("--relay", default=os.environ.get("WEBMC_RELAY") or None, metavar="URL",
                    help="fetch everything through this relay instead of directly (the Cloudflare "
                         "Worker in relay/ does this), for networks where Mojang is blocked. "
                         "\"auto\" (the default) uses the built-in relay when there is one, and "
                         "\"off\" never uses one")
    ap.add_argument("--no-jre-download", action="store_true",
                    help="never fetch a JRE; fail instead")
    ap.add_argument("--keep", action="store_true",
                    help="do not delete the sandbox at the end (debugging)")
    ap.add_argument("--cache", default=os.environ.get("WEBMC_CACHE_DIR") or None,
                    help="reuse assets and libraries from this directory instead of "
                         "downloading them again (off by default: a session normally "
                         "downloads everything into its sandbox)")
    ap.add_argument("--autoclean", type=int, default=900,
                    help="seconds after the session ends before the sandbox is deleted "
                         "(0 disables; default 900)")
    ap.add_argument("--server-only", action="store_true",
                    help="start idle: no game is launched until the page asks")
    ap.add_argument("--launch", action="store_true",
                    help="launch immediately instead of waiting for the page")
    ap.add_argument("--version", default=DEFAULT_VERSION,
                    help="Minecraft version (default %s)" % DEFAULT_VERSION)
    ap.add_argument("--loader-kind", default="fabric",
                    help="which loader: fabric (default) or none for vanilla. Forge and NeoForge "
                         "are refused until their client-patching step is implemented")
    ap.add_argument("--loader", default="auto",
                    help="loader version, or 'auto'/'stable'/'latest' for the newest suitable "
                         "build of the chosen kind")
    ap.add_argument("--server", default=None, metavar="HOST:PORT",
                    help="a Minecraft server to reach through the relay, for networks that "
                         "block game ports: the client joins it automatically")
    ap.add_argument("--tunnel-port", type=int, default=0, metavar="PORT",
                    help="the local port that receives that server (default: any free one)")
    ap.add_argument("--mods", default="",
                    help="comma-separated Modrinth slugs, e.g. fabric-api,modmenu")
    ap.add_argument("--mod-jar", action="append", default=[], metavar="PATH",
                    help="a Fabric mod jar on this machine to copy into the sandbox "
                         "(repeatable). It is deleted with the sandbox afterwards.")
    ap.add_argument("--username", default="Player")
    ap.add_argument("--memory", default=DEFAULT_MEMORY)
    ap.add_argument("--no-assets", action="store_true",
                    help="skip the ~450 MB of sounds and textures")
    ap.add_argument("--dry-run", action="store_true",
                    help="download everything, build the command, do not start the game")
    ap.add_argument("--install", action="store_true",
                    help="macOS: keep this helper in Application Support and let launchd start it "
                         "now and at every login, so nothing has to be run by hand (no admin)")
    ap.add_argument("--install-source", default=None, metavar="PATH",
                    help="with --install: the file to install (default: this one)")
    ap.add_argument("--purge", action="store_true",
                    help="remove everything of ours from this machine (cache, installed files, "
                         "login item, log, sandboxes) and say what went, then exit")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove what --install put there (and any leftover sandbox)")
    ap.add_argument("--audit", action="store_true",
                    help="say what this project has left on the machine, then exit")
    ap.add_argument("--audit-clean", action="store_true",
                    help="with --audit: remove every leftover (never a running session)")
    ap.add_argument("--store", default=os.environ.get("WEBMC_STORE") or None, metavar="DIR",
                    help="where finished sessions are kept, when keeping them is asked for "
                         "(off by default: a session belongs in the browser, and the helper "
                         "deletes the rest)")
    ap.add_argument("--keep-sessions", type=int, default=5, metavar="N",
                    help="how many finished sessions to keep, when a store is in use")
    ap.add_argument("--store-sessions", action="store_true",
                    help="keep finished sessions on disk too (the browser is the only place one is kept otherwise)")
    ap.add_argument("--forget-sessions", action="store_true",
                    help="delete the kept sessions (your worlds and options) and exit")
    ap.add_argument("--idle-exit", type=int, default=0, metavar="SECONDS",
                    help="shut down after this long with nobody using the page (0 = never; the "
                         "background launcher passes 1800 so a backgrounded helper does not "
                         "outlive its usefulness)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--print-token", action="store_true")
    return ap.parse_args(argv)


def enforce_deadlines(registry, stop, log=print, every=10.0):
    """The safety net, actually enforced.

    Two jobs, both about a helper that is still running when nobody is watching:

    * the page normally deletes the sandbox as soon as it has the session's files. If it never
      comes back - tab closed, browser crashed, visitor walked away - the helper has to do it
      itself, or the whole promise quietly fails.
    * a helper started in the background has to end when its page is gone, or "nothing left
      behind" would be true of the game and false of the thing that ran it.
    """
    while not stop.is_set():
        stop.wait(every)
        if stop.is_set():
            break
        for session in list(registry.sessions.values()):
            with session.lock:
                deadline = session.cleanup_deadline
                state = session.state
            if deadline and time.time() >= deadline and state != "running":
                log("  the page has not collected session %s by %s - deleting its sandbox"
                    % (session.sid, time.strftime("%H:%M:%S", time.localtime(deadline))))
                session.cleanup(why="nobody collected it before the deadline")
        with registry.lock:
            newest = registry.pick()
        label = newest.last_touch if newest else registry.last_touch
        idle_for = time.time() - label
        # A page that is open counts as interest even when nobody is clicking: the page pings us
        # every twenty seconds, which is what stops "left the tab open, came back to a dead helper".
        idle_limit = getattr(registry.cfg, "idle_exit", 0) or 0
        busy = any(s.state == "running" for s in list(registry.sessions.values()))
        if idle_limit and not busy and idle_for > idle_limit:
            log("  nothing has asked anything for %s - shutting down and leaving nothing behind"
                % (("%.0f minutes" % (idle_for / 60.0)) if idle_for >= 120
                   else ("%.0f seconds" % idle_for)))
            for each in list(registry.sessions.values()):
                each.cleanup(why="the helper was idle")
            # Reuse the ordinary shutdown path so the sandbox is deleted exactly once and the
            # process disappears instead of becoming a stray background process.
            os.kill(os.getpid(), signal.SIGTERM)
            return


def already_running(host, port):
    """Is the thing on that port one of ours? Asked politely, without curl."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://%s:%d/api/hello" % (host, port), timeout=3) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("name") == NAME
    except Exception:
        return False


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    cfg = argparse.Namespace(**vars(args))
    if not cfg.root:
        cfg.root = default_root()
    cfg.root = os.path.abspath(os.path.expanduser(cfg.root))

    # The helper's own root is a sandbox like any other, and it has to carry the same note: without
    # one, every restart left another directory behind that nothing could identify as ours to remove.
    try:
        os.makedirs(cfg.root, exist_ok=True)
        with open(os.path.join(cfg.root, "owner.json"), "w") as fh:
            json.dump({"pid": os.getpid(), "started": time.time(), "helper": VERSION,
                       "argv": sys.argv[1:], "boot": _boot_id(), "role": "helper root"}, fh, indent=1)
    except OSError:
        pass

    install_signal_handlers()

    # The single-file .command writes the helper out of a heredoc to a temp file. Once python has
    # read it, that copy has no reason to exist: delete it, so "it leaves nothing behind" is true
    # of the helper as well as of the game.
    _self = os.environ.get("WEBMC_SELF_DELETE")
    if _self and not args.install:
        try:
            os.unlink(_self)
        except OSError:
            pass

    if args.audit:
        audit(root=cfg.root if os.path.isdir(cfg.root) else None,
              cache=cfg.cache, clean=args.audit_clean)
        return 0

    if getattr(args, "purge", False):
        # before anything else, including the "a helper is already running" shortcut: a purge
        # that silently opened a page instead would be worse than useless
        purge(cache=cfg.cache)
        return 0

    if args.forget_sessions:
        store = session_store_dir(cfg)
        freed = 0
        kept = 0
        if os.path.isdir(store):
            for name in os.listdir(store):
                path = os.path.join(store, name)
                try:
                    freed += os.path.getsize(path)
                    os.unlink(path)
                    kept += 1
                except OSError:
                    pass
        print("  removed %d kept session(s) from %s (%.1f MB)" % (kept, store, freed / 1048576.0))
        return 0

    if args.uninstall:
        uninstall()
        return 0

    if args.install:
        source = args.install_source or globals().get("__file__")
        return install(source, page_url=args.page_url, port=args.port)

    print("")
    print("  %s %s" % (NAME, VERSION))
    print("  " + "-" * 62)
    sweep_stale_sandboxes(keep=cfg.root)

    curl = Curl()
    if args.restricted and not args.proxy:
        # in restricted mode even the proxy hunt is skipped: it is a network probe of the local
        # machine, and someone asking for this wants as little of that as possible
        proxy = None
        print("  restricted mode: not probing for a local proxy")
    else:
        proxy = detect_proxy(args.proxy, lambda m: print("  " + m))
    curl.proxy = proxy
    if not proxy:
        print("  proxy: none (using the network directly)")

    # which proxy did we end up with, and does it work at all?
    try:
        curl.get(MANIFEST_URL, dest=None, timeout=20)
        print("  network: Mojang reachable")
    except CurlError as exc:
        print("  network: NOT reachable - %s" % exc)
        print("  hint: if this machine needs a SOCKS tunnel, run it on 127.0.0.1:1080")
        print("        or pass --proxy socks5h://host:port")

    registry = Registry(cfg, curl)
    registry.started = time.time()
    page = args.page or default_page(cfg.root)
    token = secrets.token_hex(16)
    try:
        httpd = Server((args.host, args.port), Handler, registry, curl, token, page,
                       args.allow_origin, args.verbose)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            # Being already started is not an error: it is what happens when somebody opens the
            # launcher twice, or double-clicks it again after using it earlier.
            if already_running(args.host, args.port):
                url = "http://%s:%d/" % (args.host, args.port)
                print("  a helper is already running there - opening its page instead of "
                      "starting a second one")
                target = url
                if args.page_url:
                    target = "%sgo?to=%s" % (url, urllib.parse.quote(args.page_url, safe=""))
                opener = "open" if os_name() == "osx" else ("start" if os_name() == "windows"
                                                           else "xdg-open")
                try:
                    subprocess.run([opener, target])
                except Exception:
                    print("  open %s yourself" % target)
                return 0
            print("  ERROR: cannot listen on %s:%d - %s" % (args.host, args.port, exc))
            print("  hint: something else is on that port; use --port 7646")
            return 2
        raise
    port = httpd.server_address[1]
    url = "http://%s:%d/" % (args.host, port)
    print("  page:   %s" % url)
    if page and os.path.exists(page):
        print("  serving %s" % page)
    if args.page_url:
        print("  hosted: %s/go?to=%s" % (url, urllib.parse.quote(args.page_url, safe="")))
    if args.print_token:
        print("  token:  %s" % token)

    if args.open:
        opener = "open" if os_name() == "osx" else ("start" if os_name() == "windows" else "xdg-open")
        target = url
        if args.page_url:
            target = "%sgo?to=%s" % (url, urllib.parse.quote(args.page_url, safe=""))
        threading.Timer(0.6, lambda: subprocess.run([opener, target])).start()

    if args.launch:
        opts = {
            "tunnelServer": args.server,
            "tunnelPort": args.tunnel_port,
            "version": args.version,
            "loader": args.loader,
            "loaderKind": args.loader_kind,
            "relay": args.relay,
            "tunnelEverything": args.proxy_game,
            "gameProxy": args.game_proxy,
            "restricted": args.restricted,
            "mods": [m.strip() for m in (args.mods or "").split(",") if m.strip()],
            "localMods": list(args.mod_jar or []),
            "username": args.username,
            "memory": args.memory,
            "assets": not args.no_assets,
        }
        print("  launching %s%s with %s" % (
            opts["version"],
            " + " + opts["loaderKind"] if opts["loaderKind"] not in ("none", "") else " vanilla",
            ", ".join(opts["mods"]) or "no mods"))
        print("")
        registry.launch(opts)

    if not args.server_only:
        print("")
        print("  Play is a button on the page; this process only listens.")
        print("  Everything is written under %s and deleted after" % cfg.root)
        print("  the session's files have been saved. Ctrl-C to quit.")
    print("")
    try:
        stop_watchdog = threading.Event()
        watchdog = threading.Thread(
            target=enforce_deadlines, args=(registry, stop_watchdog), daemon=True)
        watchdog.start()
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        print("\n  shutting down")
    finally:
        stop_watchdog.set()
        registry.stop_all()
        if not cfg.keep:
            registry.cleanup_all()
        else:
            print("  --keep was given: %s is still there" % cfg.root)
        httpd.server_close()
        # A transient run (started by a Shortcut, say) is staged in /tmp by the file that launched
        # it. Deleting that staging directory here is what makes "nothing left behind" include the
        # helper as well as the game.
        staging = os.environ.get("WEBMC_CLEANUP_DIR")
        here = os.path.abspath(staging) if staging else None
        tempdirs = {os.path.abspath(os.environ.get("TMPDIR", "/tmp")), "/tmp", "/private/tmp",
                    os.path.abspath(tempfile.gettempdir())}
        # Only ever remove a directory this helper's own launcher made: a direct child of a temp
        # directory, named webmc.<something>. The first version of this checked for a "/tmp/" prefix
        # and therefore never fired on macOS, where the staging directory is under /var/folders.
        # webmc.XXXXXX is what mktemp makes; webmc-app.XXXXXX is what the double-clickable app makes
        # before it hands the job over. Both are ours, and both should go when this helper goes.
        staging_name = os.path.basename(here) if here else ""
        remove_staging = bool(here and (staging_name.startswith("webmc.")
                                        or staging_name.startswith("webmc-app."))
                              and os.path.dirname(here) in tempdirs and os.path.isdir(here))
        if remove_staging:
            size = 0
            for base, _dirs, files in os.walk(here):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(base, f))
                    except OSError:
                        pass
            shutil.rmtree(here, ignore_errors=True)
            print("  removed the staging directory %s (%.1f MB)" % (here, size / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
