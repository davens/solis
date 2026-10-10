#!/bin/zsh
# Renders each scenario to out/<name>.png; console output to out/<name>.log. Needs states.local.js (snapshot + config).
# THEME=walala renders that look into out/walala/ (via #<scenario>&kiosk-theme=walala).
# Headless Chrome often fails to exit after --screenshot, so wait for the PNG and kill it.
cd "$(dirname "$0")"; O=out${THEME:+/$THEME}; mkdir -p $O
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
names=(normal garage kiln-evening low-power car-cooling night all-alerts); (( $# )) && names=("$@")
for s in $names; do
  rm -f $O/$s.png
  "$CHROME" --headless=new --hide-scrollbars --window-size=1180,820 --force-device-scale-factor=1 \
    --virtual-time-budget=${BUDGET:-8000} --enable-logging=stderr --v=0 \
    --user-data-dir="${TMPDIR:-/tmp}/kiosk-chrome-$s" \
    --screenshot="$PWD/$O/$s.png" "file://$PWD/harness.html#$s${THEME:+&kiosk-theme=$THEME}" < /dev/null > $O/$s.raw 2>&1 &
  pid=$!
  for i in {1..60}; do [[ -s $O/$s.png ]] && break; sleep 0.5; done
  sleep 1; kill $pid 2>/dev/null; wait $pid 2>/dev/null
  grep -E "CONSOLE|callService" $O/$s.raw > $O/$s.log
  echo "== $s: $(ls $O/$s.png 2>&1) | console lines: $(wc -l < $O/$s.log)"; cat $O/$s.log
done
