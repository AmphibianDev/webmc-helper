#!/bin/bash
# Double-click this file in Finder to start the WebMC local helper.
#
# It starts a small HTTP server on 127.0.0.1:7645 and opens the launcher page in your
# browser. The page is only a remote control: it asks this helper to download Minecraft
# and Fabric into a throwaway directory under /tmp, starts the real Java client, and when
# you close the game it hands the session's saves back to the browser and deletes the
# directory again.
#
# Nothing is installed. No launch agent, no service, no folder anywhere but /tmp.
#
#   WEBMC_CACHE=1 ./start-helper.command    keep the ~430 MB of Mojang assets between
#                                           runs (stored in ~/Library/Caches/webmc) so
#                                           later launches are near-instant. Without it
#                                           every run downloads everything and deletes it.
#
#   ./start-helper.command --port 7646      any other helper flag also works.
#
# The window you are reading this in must stay open: closing it stops the helper, and
# stopping the helper deletes the sandbox it made.

set -u
cd "$(dirname "$0")" || exit 1

PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
  echo "python3 was not found. macOS ships it with the developer command line tools:"
  echo "    xcode-select --install"
  echo "then run this file again."
  echo
  read -r -p "press return to close " _
  exit 1
fi

ARGS=("--open" "--port" "7645")
# Open the published page, not this helper's own copy of it: the helper hands the hosted
# page its token through /go?to=…, so the tab you keep open is the one on the web.
# Set WEBMC_PAGE_URL="" to stay local, or to your own mirror.
PAGE_URL="${WEBMC_PAGE_URL-https://amphibiandev.github.io/webmc-launcher/local.html}"
if [ -n "$PAGE_URL" ]; then
  ARGS+=("--page-url" "$PAGE_URL")
fi
if [ "${WEBMC_CACHE:-0}" != "0" ]; then
  ARGS+=("--cache" "$HOME/Library/Caches/webmc")
  mkdir -p "$HOME/Library/Caches/webmc"
  echo "asset cache: $HOME/Library/Caches/webmc (delete it any time; nothing else lives there)"
fi

echo "webmc-helper - Minecraft + Fabric on this machine, kept nowhere but /tmp"
echo
if [ -n "$PAGE_URL" ]; then
  echo "page: $PAGE_URL   (this helper's own page: http://127.0.0.1:7645/, identical)"
else
  echo "page: http://127.0.0.1:7645/"
fi
echo

"$PY" webmc_helper.py "${ARGS[@]}" "$@"
STATUS=$?

echo
if [ $STATUS -ne 0 ]; then
  echo "the helper exited with status $STATUS"
fi
echo "helper stopped. its sandbox under /tmp has been removed."
read -r -p "press return to close " _
