# webmc-helper — the page only launches Minecraft; Minecraft still runs on your computer

This is the tier where **real Fabric, on a real JVM, on a real 1.21.11** runs on the visitor's own
machine. The page is a remote control. It cannot start a process, read a file, or see `/tmp` — so a
small helper that the visitor started does that, and the page drives it over `127.0.0.1`.

Everything the helper touches lands in **one directory under `/tmp`**, and the whole directory is
deleted once the session's saves have been handed to the browser:

```
page                                            127.0.0.1:7645                    /tmp/webmc-XXXX/
────                                            ──────────────                    ────────────────
probe /api/hello  ─────────────────────────────▶ hello?
POST /api/launch {version, loader, mods}  ─────▶ java -Xmx2G … KnotClient      ──▶ real Minecraft
                                                                                    + real Fabric
GET /api/log?since=N  ─────────────────────────▶ the client's own log lines
GET /api/bundle.zip  ◀───────────────────────── zip of saves/, logs/, screenshots/
     │
     ▼  stored in this browser (OPFS, per-origin, never uploaded)
POST /api/cleanup  ────────────────────────────▶ rm -rf /tmp/webmc-XXXX
     next Play: POST /api/restore ◀───────────── world goes back into a fresh /tmp sandbox
```

Nothing is installed. No launch agent, no service, no registry key, no folder anywhere but `/tmp`.

## Feedback, and reporting a problem

* The page: <https://amphibiandev.github.io/webmc-launcher/local.html>
* Issues for this helper: <https://github.com/AmphibianDev/webmc-helper/issues>

When something goes wrong, the page will hand you everything worth pasting: **How this works →
copy diagnostics** puts the helper version, the machine, the proxy in use, the sandbox path, what
is stored in the browser and the last 200 lines of log on your clipboard. The helper can also say
what it left behind:

```sh
python3 webmc_helper.py --audit
```

A report with the diagnostics pasted in can usually be answered in one round trip. If the game
itself misbehaved, the session zip (offered on the page when a session ends) contains the client's
own log and any crash report.

## Where mods go

Three places, and none of them is your home folder:

| How | What happens |
| --- | --- |
| **Drop .jar files on the Mods panel** | Kept in this browser's storage, handed to the helper before each launch, copied into the sandbox, deleted with it. Next session, the page hands them over again — so your mods travel with the browser, not with the computer. |
| **Search Modrinth in the page and press add** | The helper does the search (so it works on a network where the browser cannot reach Modrinth) and downloads a build for the version you picked, straight into the sandbox. |
| **`--mod-jar <path>` on the command line** | A jar already on this machine, for a helper started by hand. Copied in, never modified. |

`hello-mod.jar` is refused if it is not really a mod: the helper reads it and says so
("no fabric.mod.json, META-INF/mods.toml or mcmod.info inside, so this is a library or a plain
zip rather than a mod") instead of letting Fabric load nothing and leaving you to wonder.

## It leaves nothing behind — and you can check

Everything the helper creates lives in one directory, `/tmp/webmc-<random>`, and that directory is
deleted with every exit route:

| When | What deletes it |
| --- | --- |
| the page has the session's files | `POST /api/cleanup`, sent by the page itself |
| the page never comes back (tab closed, browser crashed) | the helper's watchdog, once `--autoclean` seconds (default 900) have passed since the session ended |
| you close the Terminal window, or kill the helper | signal handlers on `SIGTERM`/`SIGINT`/`SIGHUP`/`SIGQUIT` |
| the helper was killed outright, or the machine went down | the next helper to start reads the sandbox's `owner.json`, sees that the process which made it is gone, and deletes it there and then |

To see it for yourself:

```sh
python3 webmc_helper.py --audit                    # every item this helper has on the machine
python3 webmc_helper.py --audit --cache ~/Library/Caches/webmc
python3 webmc_helper.py --audit --audit-clean      # remove leftovers; touches nothing else
```

```
  webmc-helper 0.1.0 - what is on this machine
  ----------------------------------------------------------
  nothing: no sandbox, no stray file. /tmp has none of ours.
```

The audit claims only what this helper makes — a sandbox, or a stray curl config — and reports
anything else that merely shares the `webmc-` prefix as a count it does not touch.

## Run it on this machine

There is no terminal in this path, on purpose. Download **[WebMC Helper.zip]**, unzip it, and
double-click **WebMC Helper.app**. That is the whole setup:

1. it copies itself into `~/Library/Application Support/webmc`,
2. writes one user launch agent (a plist in `~/Library/LaunchAgents`),
3. asks launchd to start it now and at every login,
4. opens the launcher page.

From then on the page finds a helper already running. Close the app, close the browser, reboot:
it is there. The game still runs in a throwaway directory under `/tmp` and that directory is
deleted when a session ends — the only things that persist are those two small files, and
`--uninstall` removes both:

```sh
python3 "$HOME/Library/Application Support/webmc/helper/webmc_helper.py" --uninstall
python3 "$HOME/Library/Application Support/webmc/helper/webmc_helper.py" --audit
```

**Nothing here needs an administrator.** macOS runs a user's own launch agents without one, and
this writes only inside your home directory and `/tmp`. What it touches, verbatim from `--audit`:

```
  writes to            /tmp/webmc-<random>/ - and nothing else; that path is deleted when the session ends
  privileges           none: no sudo, no administrator, no installer, no write outside /tmp and your own home
  installed            /Users/you/Library/Application Support/webmc
                       /Users/you/Library/LaunchAgents/dev.amphibian.webmc-helper.plist
                       installed on purpose so the page always finds a helper; remove both with --uninstall
```

The first launch may still meet Gatekeeper ("Apple could not verify..."). Right-click the app and
choose **Open** once; that is the cost of an unsigned download, and a Developer ID signature is
what removes it.

In the zip: the app (which carries the helper, the page it serves, and a
`helper/start-helper.command` for anyone who prefers a terminal) and this README. Nothing else —
no tests, no templates, no build output, and a drift test asserts that file list.

For anyone starting it by hand instead, everything below still works.

```sh
cd "$HOME/Library/Application Support/webmc/helper"   # or wherever you kept it
./start-helper.command            # Finder: runs in the background and closes its own window
WEBMC_FOREGROUND=1 ./start-helper.command             # or watch it in a terminal
python3 webmc_helper.py --install                     # the app's own install step
python3 webmc_helper.py --help
```

The useful flags, when started by hand:

```sh
python3 webmc_helper.py --server-only                 # wait for the page instead of launching now
python3 webmc_helper.py --launch --mods fabric-api,modmenu
python3 webmc_helper.py --launch --loader 0.19.5 --no-assets
python3 webmc_helper.py --launch --mod-jar ~/Downloads/sodium.jar
WEBMC_CACHE=1 ./start-helper.command                  # keep the asset cache between runs
python3 webmc_helper.py --print-token --page-url https://derek.github.io/webmc/
```

## The page works in two places

| Page lives | How it finds the helper |
| --- | --- |
| **Served by the helper** (`http://127.0.0.1:7645/`) | The page's own origin *is* the helper; the helper injects its token into the page's `<head>`. Same-origin, so no permission prompt and no CORS. This is the path that always works. |
| **Hosted** (`https://amphibiandev.github.io/webmc-launcher/local.html`) | `--page-url https://…` prints a one-time `<helper>/go?to=<page>` link. That redirect hands the page `#helper=…&t=<token>`, which the page keeps in `localStorage`. Cross-origin, so it also needs the Local Network Access permission described below. |

`start-helper.command` opens the hosted page by default and prints both URLs; set
`WEBMC_PAGE_URL=""` to open the local one instead.

Either way the page never probes on load. Without a stored connection it waits for a click, and a
failed probe is remembered and not repeated. That matters because of the next section.

## Why a browser cannot do this alone (and why localhost is treated carefully)

A page cannot spawn a process or read `/tmp`. There is no API for it, and no amount of cleverness in
JavaScript changes that: WebAssembly and an in-page JVM (CheerpJ) are sandboxed, `file://` gives an
opaque origin that cannot even reach `127.0.0.1`, a protocol handler and an extension both need an
installer first, and an AOT-compiled client (TeaVM, Eaglercraft's route) can never load a mod jar at
play time because it has no class loader.

So the helper is the only way to get *play-time* mods on a desktop, and it deliberately stays small
and boring:

* **Token.** Every `/api/*` call except `/api/hello` needs `X-WebMC-Token`, which is random per run
  and handed to the page once. Without it the endpoint is not reachable from a random tab.
* **Origin allowlist.** CORS echoes only `http://localhost:*`, `http://127.0.0.1:*`, `https://*.github.io`
  and `https://*.pages.dev` (plus `--allow-origin`). Any other Origin gets no CORS headers.
* **Local Network Access.** Chrome 142+ and Edge 143+ put requests from a public site to `127.0.0.1`
  behind the new Local Network Access permission prompt, and admins can block it outright with the
  `LocalNetworkAccessBlockedForUrls` policy. The helper answers the preflight properly
  (`Access-Control-Allow-Private-Network: true`), which is necessary but not sufficient — where an
  admin has blocked loopback, no page-side bridge of any kind will work and only a browser-only tier
  does. That is exactly why this helper is *not* the default path for locked-down machines.
* **Cleanup on every exit path.** `SIGTERM`, `SIGINT` and `SIGHUP` (closing the Terminal window) all
  delete the sandbox, and at startup the helper sweeps `/tmp/webmc-*` directories older than six
  hours, in case a machine went down mid-session.

## What it does, in order

1. Finds a Java 21 runtime. It reuses one you already have (`~/.jdks/*/Contents/Home`, `$JAVA_HOME`,
   `/Library/Java/JavaVirtualMachines`, Homebrew, `PATH`) and only fetches a Temurin JRE into the
   sandbox — `--no-jre-download` refuses instead.
2. Reads Mojang's version manifest and the version's own JSON.
3. Downloads the client jar, evaluates the `rules` on every library (os, arch, version, features) and
   fetches what applies. **On Apple Silicon it drops `natives-macos` when `natives-macos-arm64`
   exists** — Mojang gives both the same rules, and `natives-macos-patch` is x86_64-only, so a naive
   rule evaluator puts an Intel dylib on the classpath and the client dies.
4. Downloads the asset index and its objects (parallel `curl`, sha1-verified, `--create-dirs`).
5. Fetches Fabric's loader profile, merges its libraries and `mainClass` with Mojang's JVM/game
   arguments, and substitutes every `${placeholder}` — including `-XstartOnFirstThread`, which arrives
   with an `osx` rule.
6. Fetches Modrinth mods for the chosen version, and copies any `--mod-jar` files, into `mods/`.
7. Starts the client with an offline identity (`OfflinePlayer:<name>` → UUID v3). Singleplayer works;
   online servers need a real login, which this does not do.
8. On exit, collects `saves/ logs/ crash-reports/ screenshots/ config/ options.txt servers.dat` plus
   the client's console log and the exact launch command into one zip, reports it, and waits for the
   page to take it before deleting the sandbox.

## What it does not do

* **No online-mode login.** Offline usernames only; premium servers will reject the join.
* **Nothing runs anywhere but here.** No server session, no stream, no relay — see
  `webmc-port/COMPLIANCE.md` for why that line is not crossed.
* **No telemetry, no uploads.** The session zip goes into the browser's own storage and nowhere else.
* **Not for locked-down machines.** Where a visitor cannot run any program, this cannot run either;
  that is what the browser-only tier in `webmc-port/` is for.
* **macOS-shaped, not macOS-only.** Java detection, native extraction and the `.command` launcher are
  built and tested on Apple Silicon. Linux and Windows should work (`%TEMP%` instead of `/tmp` is the
  main difference) but have not been tried.

## Tests

`tests/hello-mod/` is a Fabric mod compiled on this machine against a `fabric-loader` jar, used to
prove that a jar this machine built is loaded by Fabric inside Minecraft at play time:

```sh
cd tests/hello-mod && ./build.sh          # -> hello-mod.jar (~6 KB)
python3 ../helper/webmc_helper.py --launch --loader 0.19.5 --mod-jar hello-mod.jar
grep WEBMC-DEMO-MOD /tmp/webmc-*/game/logs/latest.log
```

What a verified session looks like in the client's own log:

```
Loading Minecraft 1.21.11 with Fabric Loader 0.19.5
Loading 5 mods:
	- fabricloader 0.19.5
	- java 21
	- minecraft 1.21.11
	- webmc-demo 1.0.0
[Render thread/INFO]: [STDOUT]: WEBMC-DEMO-MOD: loaded from a locally compiled jar, by Fabric, at play time
[Render thread/INFO]: Backend library: LWJGL version 3.3.3-snapshot
[Render thread/INFO]: Sound engine started
```

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| The hosted page says **nothing answered on 127.0.0.1** although the helper is running | Chromium 142+ / Edge 143+ gate a public page's request to loopback behind the **Local Network Access** permission. In a normal browser Chrome asks once — allow it (padlock icon → Site settings → Local network access). Headless and automated browsers have no prompt to click, so they fail closed. Where an administrator sets `LocalNetworkAccessBlockedForUrls` the request is refused outright and no permission can be granted. |
| …and you would rather not grant anything | Use the helper's own page: `http://127.0.0.1:7645/`. It is the same page, from the same file, and being same-origin with the helper it needs no permission and no token handoff. The hosted page carries a link to it for exactly this reason. |
| `the network is not reachable` at startup | Some networks answer DNS for `piston-meta.mojang.com` with `0.0.0.0`. The helper auto-detects a SOCKS5 proxy on `127.0.0.1:1080`, `:1088`, `:10808`, `:7890`, or take `--proxy socks5h://host:port`. `--proxy none` forces a direct connection. `socks5h` matters: it resolves names at the proxy, so a poisoned resolver does not matter. With `--cache` a launch needs no network at all. |
| `port 7645 is in use` | `--port 7646`, or quit the other helper. |
| Page says "not connected" although the helper runs | The token is from an older helper run. Reload the page, or re-open the `/go?to=…` link. |
| `EXC_BAD_INSTRUCTION` / wrong architecture dylib | A `natives-macos` jar for the wrong arch got onto the classpath; the helper filters these, so report it rather than work around it. |
| Game starts but is silent and textureless | Assets were skipped (`--no-assets`) or failed to download; the log says which. |
| Everything is slow the first time | ~430 MB of assets, sha1-verified. Use `WEBMC_CACHE=1` for later runs. |
