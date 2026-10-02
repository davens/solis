#!/bin/zsh
# Renders each scenario to out/<name>.png; console output to out/<name>.log. Needs states.local.js (snapshot + config).
# Headless Chrome often fails to exit after --screenshot, so wait for the PNG and kill it.
cd "$(dirname "$0")"; mkdir -p out
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
names=(normal garage kiln-evening low-power car-cooling night all-alerts); (( $# )) && names=("$@")
for s in $names; do
  rm -f out/$s.png
  "$CHROME" --headless=new --hide-scrollbars --window-size=1180,820 --force-device-scale-factor=1 \
    --virtual-time-budget=${BUDGET:-8000} --enable-logging=stderr --v=0 \
    --user-data-dir="${TMPDIR:-/tmp}/kiosk-chrome-$s" \
    --screenshot="$PWD/out/$s.png" "file://$PWD/harness.html#$s" < /dev/null > out/$s.raw 2>&1 &
  pid=$!
  for i in {1..60}; do [[ -s out/$s.png ]] && break; sleep 0.5; done
  sleep 1; kill $pid 2>/dev/null; wait $pid 2>/dev/null
  grep -E "CONSOLE|callService" out/$s.raw > out/$s.log
  echo "== $s: $(ls out/$s.png 2>&1) | console lines: $(wc -l < out/$s.log)"; cat out/$s.log
done
