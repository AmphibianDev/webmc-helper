-- The launcher a visitor double-clicks, with no Terminal window anywhere in it.
--
-- A .command file always opens a shell window; this is the same work wrapped in an app bundle, built
-- from this script by tools/publish-helper.py with osacompile. The whole first run is: download,
-- double-click, done. What it leaves is two small files in the user's Library and one login item that
-- keeps the helper alive from then on - and "Leave nothing" in the page removes all of it.

set helperArchive to "https://amphibiandev.github.io/webmc-launcher/helper/webmc-helper.zip"
set launcherPage to "https://amphibiandev.github.io/webmc-launcher/local.html"
set probeCommand to "curl -fsS -m 3 http://127.0.0.1:7645/api/hello 2>/dev/null | grep -q webmc-helper"

-- Already running? Then a second double-click is not an error: open the page and stop.
try
	do shell script probeCommand
	open location launcherPage
	return "WebMC is already running. Your launcher page is open."
end try

-- One directory under /tmp, and everything happens there.
set work to do shell script "mktemp -d /tmp/webmc-app.XXXXXX"
set runCommand to "cd " & quoted form of work & " && curl -fsSL " & quoted form of helperArchive & " -o h.zip && ditto -x -k h.zip . && rm h.zip && WEBMC_PAGE_URL=" & quoted form of launcherPage & " ./webmc-helper.command --install >/dev/null 2>&1 &"

try
	do shell script runCommand
on error errText number errNum
	return "WebMC could not start: " & errText & " (" & errNum & ")"
end try

-- Wait for it to bind its port, tidy up after ourselves, then hand the tab over.
repeat 40 times
	delay 0.5
	try
		do shell script probeCommand
		do shell script "rm -rf " & quoted form of work
		open location launcherPage
		return "WebMC is installed and running. Your launcher page is open."
	end try
end repeat

do shell script "rm -rf " & quoted form of work
open location "http://127.0.0.1:7645/"
return "WebMC was installed but is not answering yet. Opening its own page."
