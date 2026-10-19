#!/usr/bin/env python3
"""A stand-in for the Cloudflare Worker, so the helper's --relay path can be tested with no
network to Mojang at all (which is the situation it exists for).

It answers the same prefixes as relay/relay.js and serves the files a launch needs out of a local
cache that a previous, working launch left behind:

    python3 relay/test-relay.py --root ~/Library/Caches/webmc --port 8787
    python3 helper/webmc_helper.py --launch --relay http://127.0.0.1:8787 \
        --root /tmp/webmc-relay-test --no-assets

Nothing here is part of the real thing: the Worker fetches from the internet and caches at the edge.
This fetches from a directory and is only here to prove the helper talks to a relay correctly.
"""

import argparse
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIXES = ("piston-meta", "piston-data", "resources", "libraries", "fabric-maven",
            "fabric-meta", "forge-maven", "neoforge-maven", "modrinth", "adoptium")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    root = None
    version = "1.21.11"
    loader = "0.19.5"

    def log_message(self, fmt, *args):
        sys.stderr.write("  relay <- %s\n" % (fmt % args))

    # ------------------------------------------------------------------ helpers
    def _send(self, code, blob, ctype="application/octet-stream", head=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not head:
            self.wfile.write(blob)

    def _json(self, code, obj, head=False):
        self._send(code, json.dumps(obj).encode(), "application/json", head)

    def _file(self, path, head=False):
        if path and os.path.isfile(path):
            with open(path, "rb") as fh:
                self._send(200, fh.read(), head=head)
            return True
        return False

    # ------------------------------------------------------------------ routing
    def _serve(self, head):
        parts = urllib.parse.urlsplit(self.path)
        bits = [b for b in parts.path.split("/") if b]
        query = urllib.parse.parse_qs(parts.query)
        if not bits:
            self._json(200, {"ok": True, "service": "webmc test relay",
                             "prefixes": list(PREFIXES)}, head)
            return
        prefix, rest = bits[0], bits[1:]
        root, version = self.root, self.version
        if prefix not in PREFIXES:
            self._json(404, {"error": "unknown prefix", "prefix": prefix}, head)
            return

        # ---- the files a launch asks for, in the order it asks for them
        if prefix == "piston-meta" and rest == ["mc", "game", "version_manifest_v2.json"]:
            self._json(200, {
                "latest": {"release": version, "snapshot": version},
                "versions": [{"id": version, "type": "release",
                              "url": "http://%s/piston-meta/%s.json" % (self.headers.get("Host"), version)}],
            }, head)
            return
        if prefix == "piston-meta" and rest == ["%s.json" % version]:
            if self._file(os.path.join(root, "versions", "%s.json" % version), head):
                return
        if prefix == "piston-data" and len(rest) >= 3 and rest[0] == "v1" and rest[1] == "objects":
            if self._file(os.path.join(root, "versions", rest[-1]), head):
                return
        if prefix == "resources" and len(rest) == 2:
            if self._file(os.path.join(root, "assets", "objects", rest[0], rest[1]), head):
                return
        if prefix == "libraries":
            if self._file(os.path.join(root, "libraries", *rest), head):
                return
        if prefix == "fabric-meta" and rest == ["v2", "versions", "loader", version]:
            self._json(200, [{"loader": {"version": self.loader, "stable": True},
                              "intermediary": {"version": version}, "launcherMeta": {}}], head)
            return
        if prefix == "fabric-meta" and len(rest) == 6 and rest[-1] == "json":
            if self._file(os.path.join(root, "fabric", "%s-%s.json" % (version, rest[4])), head):
                return
        if prefix == "fabric-maven":
            if self._file(os.path.join(root, "libraries", *rest), head):
                return
        if prefix == "adoptium":
            self._json(404, {"error": "this test relay does not serve JREs"}, head)
            return
        if prefix == "modrinth":
            self._json(200, {"hits": []}, head)
            return

        self._json(404, {"error": "not in this test relay", "path": parts.path}, head)

    def do_GET(self):
        self._serve(False)

    def do_HEAD(self):
        self._serve(True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/Library/Caches/webmc"))
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--version", default="1.21.11")
    ap.add_argument("--loader", default="0.19.5")
    args = ap.parse_args()
    Handler.root, Handler.version, Handler.loader = args.root, args.version, args.loader
    missing = [p for p in ("versions/%s.json" % args.version, "versions/client.jar",
                           "fabric/%s-%s.json" % (args.version, args.loader))
               if not os.path.exists(os.path.join(args.root, p))]
    if missing:
        print("this relay needs a warm cache first (missing: %s)" % ", ".join(missing))
        print("run one normal launch with --cache, then start the relay")
        return 1
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("test relay on http://127.0.0.1:%d serving %s (nothing reaches Mojang)" % (args.port, args.root))
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
