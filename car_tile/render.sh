#!/bin/bash
# usage: ./render.sh <template.js> <outdir>  -> renders scenarios, then <outdir>/preview.png
set -e
cd "$(dirname "$0")"
node harness.js "$1" "$2"
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 --window-size=520,1500 --virtual-time-budget=4000 \
  --screenshot="$(cd "$2" && pwd)/preview.png" "file://$(cd "$2" && pwd)/preview.html" >/dev/null 2>&1
echo "png: $2/preview.png"
