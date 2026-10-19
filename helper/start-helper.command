#!/bin/bash
# Double-click this file in Finder to start the WebMC helper.
#
# It starts a small HTTP server on 127.0.0.1:7645 and opens the launcher page. The page is only a
# remote control: it asks this helper to download Minecraft and Fabric into a throwaway directory
# under /tmp, starts the real Java client, and when you close the game it hands the session's saves
# back to the browser and deletes the directory again.
#
# WHAT THIS NEEDS
#   Nothing privileged. No sudo, no installer, no launch agent, no administrator account, no write
#   anywhere except /tmp. It listens on 127.0.0.1, so macOS will not even ask about incoming
#   connections. You can run it on a standard, non-admin account and check with:
#       python3 webmc_helper.py --audit
#   which prints what it writes to and confirms the privilege line above.
#
# IT RUNS IN THE BACKGROUND
#   This window closes itself: the helper is detached and keeps running, and it shuts down on its
#   own shortly after nothing is using it (or as soon as the launcher page has saved your session).
#   To watch it in the foreground instead:
#       WEBMC_FOREGROUND=1 ./start-helper.command
#
# OPTIONS
#   WEBMC_CACHE=1 ./start-helper.command     keep the ~430 MB of Mojang assets between runs
#                                            (in ~/Library/Caches/webmc) so later launches are
#                                            near-instant. Without it every run downloads what it
#                                            needs and deletes it afterwards.
#   WEBMC_FOREGROUND=1 ./start-helper.command   run here, with the log in this window
#   ./start-helper.command --port 7646       any helper flag also works
#   WEBMC_PAGE_URL="" ./start-helper.command open the helper's own page, not the published one

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
# Open the published page, not this helper's own copy of it: the helper hands the hosted page its
# token through /go?to=…, so the tab you keep open is the one on the web. Set WEBMC_PAGE_URL=""
# to stay local, or to your own mirror.
PAGE_URL="${WEBMC_PAGE_URL-https://amphibiandev.github.io/webmc-launcher/local.html}"
if [ -n "$PAGE_URL" ]; then
  ARGS+=("--page-url" "$PAGE_URL")
fi
if [ "${WEBMC_CACHE:-0}" != "0" ]; then
  ARGS+=("--cache" "$HOME/Library/Caches/webmc")
  mkdir -p "$HOME/Library/Caches/webmc"
fi

if [ "${WEBMC_FOREGROUND:-0}" != "0" ]; then
  echo "webmc-helper - foreground; close this window to stop it and delete its sandbox"
  [ -n "$PAGE_URL" ] && echo "page: $PAGE_URL   (or http://127.0.0.1:7645/)"
  exec "$PY" -u webmc_helper.py "${ARGS[@]}" "$@"
fi

# Already running? Then this is a second double-click, which is not a failure: say so and open
# the page, rather than starting a second helper on a busy port and reporting an error.
if WEBMC_PROBE_PORT=7645 "$PY" -c 'import os,socket,sys
s=socket.socket(); s.settimeout(0.4)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(os.environ["WEBMC_PROBE_PORT"])))==0 else 1)' 2>/dev/null; then
  echo "a webmc-helper is already running on 127.0.0.1:7645 - using that one."
  echo "It stops by itself shortly after nothing is using it, and it deletes everything"
  echo "it downloaded when a session ends. To stop it now:  pkill -f webmc_helper.py"
  TARGET="http://127.0.0.1:7645/"
  [ -n "$PAGE_URL" ] && TARGET="http://127.0.0.1:7645/go?to=$PAGE_URL"
  "$PY" -c 'import sys,webbrowser; webbrowser.open(sys.argv[1])' "$TARGET" >/dev/null 2>&1 || true
  exit 0
fi

# Background: detached, no window to keep open, and it ends by itself when nobody is using it.
LOG="${TMPDIR:-/tmp}/webmc-helper-boot.log"
nohup "$PY" -u webmc_helper.py "${ARGS[@]}" --idle-exit 1800 "$@" >"$LOG" 2>&1 &
HELPER_PID=$!
sleep 2

if kill -0 "$HELPER_PID" 2>/dev/null; then
  echo "webmc-helper is running in the background (pid $HELPER_PID)."
  echo "This window can close - it is not keeping anything alive."
  echo
  echo "It stops by itself shortly after nothing is using it, and it deletes"
  echo "everything it downloaded when a session ends. To stop it now:"
  echo "    kill $HELPER_PID"
  echo
  echo "The launcher page should be opening in your browser."
  echo "http://127.0.0.1:7645/  (the same page, served locally)"
  sleep 2
else
  echo "the helper did not start. What it said:"
  echo
  tail -25 "$LOG" 2>/dev/null
  echo
  read -r -p "press return to close " _
  exit 1
fi
