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

## What this asks of the machine

| | |
| --- | --- |
| **bash, curl, tar** | required - all three ship with macOS and with Windows 10+ |
| **Python** | only used if the machine already has it; otherwise a relocatable CPython (26 MB on macOS, 46 MB on Windows) is fetched into the session's own folder |
| **Java** | never required - the JRE is fetched into that same folder |
| **Xcode command line tools, Homebrew, administrator rights** | never |

So a machine with nothing but a stock shell can run this. Everything it downloads - the Python
runtime, the helper, the Java runtime, the client, the libraries, the assets - goes into one
staging directory under the temp folder, and the helper deletes the whole directory when the
session ends. Measured on a Mac with no Python and no Java:

```
This machine has no Python 3, so webmc-helper is fetching one into its own folder:
    /var/folders/.../T/webmc.KHQ3rU
Nothing is installed, and it is deleted with the rest when the session ends.
...
Loading Minecraft 1.21.11 with Fabric Loader 0.19.5
Sound engine started
--- after the session --- staging gone: yes   sandbox gone: yes   0 left behind
```

## Why not just use SKLauncher?

Honest answer: **if you want a launcher, use a launcher.** [SKLauncher](https://sklauncher.com/),
[Prism Launcher](https://prismlauncher.org/), [MultiMC](https://multimc.org/),
[ATLauncher](https://atlauncher.com/), [Modrinth App](https://modrinth.com/app) and Mojang's own
launcher are all better at being launchers than this is: more features, more loaders, more help
when something goes wrong. Nothing here replaces them, and the README of any project that claimed
otherwise would be lying.

What this does that they do not:

* **Leaves nothing behind.** The game, the Java runtime, the Python runtime and the helper live in
  one temp directory that is deleted when the session ends, and your world lives in your browser.
  A normal launcher installs itself, keeps `~/.minecraft`, and stays.
* **Needs nothing installed.** No Java, no Python, no Xcode tools, no administrator. A stock Mac
  or Windows machine with a shell is enough.
* **Is a web page.** The interface can be updated without anybody re-downloading anything, and the
  same page can drive a session on any machine that has the helper running.

Use a real launcher when the machine is yours and you want a permanent installation with all the
features. Use this when you want to play *with mods* on a machine you would rather not change -
a borrowed laptop, a school machine you are allowed to run things on, or your own computer when
you simply do not want a Minecraft folder in it.

## Two ways to start it, both leaving nothing behind

**The clean one, on a Mac: a Shortcut.** It fetches the helper into `/tmp` and runs it there, so
nothing of ours is ever installed: no download kept, no launch agent, no staging directory. When
the session ends the helper deletes its own folder, and `/tmp` is as it was.

Make it once (about a minute, no download, no untrusted-shortcut settings):

1. Open **Shortcuts**, press **+**.
2. Add the action **Run Shell Script**.
3. Set **Pass Input** to **to stdin**, and paste this:

```sh
D=$(mktemp -d /tmp/webmc.XXXXXX) && cd "$D" && \
  curl -fsSL https://amphibiandev.github.io/webmc-launcher/helper/webmc-helper.zip -o h.zip && \
  ditto -x -k h.zip . && rm h.zip && \
  WEBMC_TRANSIENT=1 \
  WEBMC_PAGE_URL=https://amphibiandev.github.io/webmc-launcher/local.html \
  ./webmc-helper.command
```

4. Name it exactly **WebMC Helper** and save. The first run asks once for
   **Shortcuts → Settings → Advanced → Allow Running Scripts**.

Then the launcher page can start it for you: press *Start the helper for me*, allow the one
"Open Shortcuts?" prompt, and the page waits until the helper answers before enabling Play.

A downloadable `.shortcut` file is *not* the recommended route: `shortcuts` can run, list and
sign, but it cannot create or import, and importing somebody else's shortcut needs macOS's hidden
**Allow Untrusted Shortcuts** toggle. Four steps you do once beat a toggle you cannot find.

**The other way: install it once.** One double-click, then it starts at every login (below).
More convenient, two small files persist, `--uninstall` removes them.

## Run it on this machine

**One file, nothing installed.** Download `webmc-helper.zip`, double-click it to unzip, then
double-click **`webmc-helper.command`**. That is the whole thing:

* it starts in the background and its window closes itself,
* the launcher page opens, and pressing **Play** there starts Minecraft on your desktop,
* nothing is added to your home directory, no login item, no daemon,
* it exits by itself when a session ends (or after half an hour of nobody using it), and the
  folder it downloads the game into is deleted — including after a crash,
* delete the file whenever; there is nothing else to undo.

```sh
WEBMC_FOREGROUND=1 ./webmc-helper.command    # watch it in the terminal instead
WEBMC_CACHE=1 ./webmc-helper.command         # keep the ~430 MB of Mojang assets between runs
./webmc-helper.command --help                # every flag still works
```

Why a `.command` and why inside a zip: macOS gives a downloaded script **no executable bit** —
measured, in a real browser — so a bare download cannot be double-clicked at all, and an `.app`
is a directory rather than a file. A zip is the only thing that carries the bit across, and the
script is the only single file macOS will run on a double-click without installing anything.

## Mod loaders

**Fabric, for now.** It is the loader most mods are published for, and it is the one that works
end to end here: any version Fabric supports, mods fetched from Modrinth by slug or handed over as
`.jar` files, loaded at play time by a real JVM.

**Forge and NeoForge are not supported yet.** The resolution is written and was verified: the
build list comes from their Maven, the version document is read straight out of their own
installer jar, the libraries download and the launch command is built — FML starts and loads its
early services. But neither loader ships a usable client jar: their installer runs
`installertools` over the vanilla jar to produce `client-<mc>-<stamp>-srg.jar` and `...-extra.jar`,
and the bootstrap refuses to start without them. Until the helper runs that patch step, asking for
those loaders is refused with that explanation rather than half-launched.

Doing it needs no Java networking: every input can be fetched with curl (which speaks the proxy
properly, unlike Java's SOCKS client, which fails against an SSH tunnel with "Malformed reply from
SOCKS server") and the patch step run against local files.

## Where your mods go

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
## The relay, if a network blocks things

Optional, and the difference between "the game will not download" and a normal session on a network
that blocks Mojang or game ports. It is a Cloudflare Worker: it fetches from Cloudflare's network
instead of yours, and it dials a Minecraft server on your behalf.

**You only deploy one if you want your own.** The helper has a built-in relay list (`DEFAULT_RELAYS`
in `webmc_helper.py`), probed once per run, used when the page does not name a relay, and skipped the
moment one is unreachable — so a normal network downloads directly and never notices. Put a deployed
URL in that tuple and everyone using this helper gets it: nobody else has to deploy, log in or
configure anything. The page's **Relay** setting is then just a choice of *Automatic* (whatever the
helper has), *Never*, or *My own relay*.

```bash
cd webmc-helper
npx wrangler deploy relay/relay.js --name webmc-relay --compatibility-date 2026-01-01
```

That needs a free Cloudflare account and a browser login the first time (`npx wrangler login`). It
prints a URL like `https://webmc-relay.you.workers.dev`; paste it into **Settings → If a network
blocks things → Relay address** and press **Test the relay**.

Check it from the command line, from the network that was blocked:

```bash
python3 relay/test-relay.py https://webmc-relay.you.workers.dev
```

It asks the relay for Mojang's version list (the check that proves the block is gone), confirms the
policy refusals, gets a token for a real server, and asks a real server for its status through the
tunnel. It uses the same tunnel code the launcher does, so a pass there is a pass in the game.

What it does:

| Route | What happens |
|---|---|
| `/<prefix>/<path>` | Mapped to the real host (`piston-meta`, `piston-data`, `resources`, `libraries`, `fabric-maven`, `fabric-meta`, `forge-maven`, `neoforge-maven`, `modrinth`, `adoptium`), streamed back, and cached at Cloudflare's edge, so the origin is hit once per file rather than once per visitor. |
| `POST /v1/token` | A signed, two-minute token for one host and port. |
| `GET /v1/connect` | A WebSocket speaking `goldproxy.v1`: the game's bytes travel inside ordinary HTTPS. |
| `GET /health` | What this deployment offers — what the page's **Test the relay** button reads. |

It also refuses to be a port scanner: a session has to open with a Minecraft handshake, the port has
to be one this relay dials, and private, loopback, link-local and cloud-metadata addresses are
refused. The knobs, if you want them (wrangler variables):

| Variable | Default | Meaning |
|---|---|---|
| `RELAY_SECRET` | a fixed development string | Signing key for tokens. **Set this** on a public deployment. |
| `RELAY_ALLOWED_PORTS` | 25565, 25566, 25567, 25568, 25575, 25585, 19132 | Ports a session may dial. |
| `RELAY_ALLOW_PRIVATE` | `0` | `1` allows LAN and loopback targets. |
| `RELAY_ALLOW_ANY_PORT` | `0` | `1` stops enforcing the port allowlist. |
| `RELAY_MAX_SECONDS` | `1800` | Longest a single session may live. |

The helper uses it when it is given `--relay URL` (downloads), `--server HOST:PORT` (a server to join
through the tunnel) or `--proxy-game` (the game's own connections). The launcher's Settings panel
sets all three from the one URL, which is the whole point of it being one setting.

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
