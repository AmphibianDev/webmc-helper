#!/bin/bash
# Builds hello-mod.jar against a fabric-loader jar, using the JDK we already have.
# Usage: ./build.sh [fabric-loader.jar] [output.jar]
set -euo pipefail
cd "$(dirname "$0")"
LOADER="${1:-}"
OUT="${2:-hello-mod.jar}"
JDK="${WEBMC_JDK:-$HOME/.jdks/jdk-21.0.12.1+1/Contents/Home}"
[ -x "$JDK/bin/javac" ] || JDK="$(/usr/libexec/java_home -v 21 2>/dev/null || echo '')"
[ -x "$JDK/bin/javac" ] || { echo "no JDK 21 found; set WEBMC_JDK"; exit 1; }
if [ -z "$LOADER" ]; then
  LOADER="$(ls -d "$HOME"/Library/Application\ Support/webmc* 2>/dev/null | head -1 || true)"
  LOADER="${LOADER:-/tmp/webmc-cache/libraries/net/fabricmc/fabric-loader/0.19.5/fabric-loader-0.19.5.jar}"
fi
[ -f "$LOADER" ] || { echo "fabric-loader jar not found at $LOADER"; exit 1; }
rm -rf out "$OUT"
mkdir -p out
"$JDK/bin/javac" -cp "$LOADER" -d out src/webmc/demo/HelloMod.java
cp fabric.mod.json out/
( cd out && "$JDK/bin/jar" --create --file "../$OUT" . ) 2>/dev/null || \
  python3 -c "import zipfile,os;z=zipfile.ZipFile('$OUT','w',zipfile.ZIP_DEFLATED);[z.write(os.path.join(r,f),os.path.relpath(os.path.join(r,f),'out')) for r,_,fs in os.walk('out') for f in fs];z.close()"
echo "built $OUT"
python3 -c "import zipfile;print('  contents:', zipfile.ZipFile('$OUT').namelist())"
