#!/usr/bin/env bash
# Reproduce the REPORT.md resource measurements (no root needed).
# Runs collector + web as transient systemd *user* services with the same
# limits as deploy/*.service, then samples cgroup CPU time and memory while
# 0, 1 and 10 browsers hold the live SSE stream open.
#   usage: deploy/measure.sh <session-cookie>   (cookie of a signed-in admin, from the browser)
set -u
cd "$(dirname "$0")/.."
COOKIE=${1:?pass the hsm_session cookie value of a signed-in user}
PORT=${BACKEND_PORT:-8000}
R=measure-results.txt; : > $R
systemd-run --user --unit=hsm-m-collector --working-directory="$PWD/backend" -p MemoryHigh=96M -p MemoryMax=128M \
  -p CPUQuota=10% -p TasksMax=16 -p Nice=10 -p NoNewPrivileges=yes "$PWD/.venv/bin/python" -m hsm.collector
systemd-run --user --unit=hsm-m-web --working-directory="$PWD/backend" -p MemoryHigh=128M -p MemoryMax=192M \
  -p CPUQuota=25% -p TasksMax=64 -p NoNewPrivileges=yes "$PWD/.venv/bin/python" -m hsm.web
trap 'systemctl --user stop hsm-m-collector hsm-m-web' EXIT
until curl -s -o /dev/null "localhost:$PORT/"; do sleep 0.5; done
cpu() { systemctl --user show "$1" -p CPUUsageNSec --value; }
mem() { local pid; pid=$(systemctl --user show "$1" -p MainPID --value)
  echo "RSS $(ps -o rss= -p "$pid" | awk '{printf "%.1f MiB",$1/1024}'), cgroup $(systemctl --user show "$1" -p MemoryCurrent --value | awk '{printf "%.1f MiB",$1/1048576}')"; }
win() { local a b c d; a=$(cpu hsm-m-collector); c=$(cpu hsm-m-web); sleep "$2"; b=$(cpu hsm-m-collector); d=$(cpu hsm-m-web)
  printf '%-18s collector %6s%% (%s) | web %6s%% (%s)\n' "$1" \
    "$(awk "BEGIN{printf \"%.3f\", ($b-$a)/($2*1e9)*100}")" "$(mem hsm-m-collector)" \
    "$(awk "BEGIN{printf \"%.3f\", ($d-$c)/($2*1e9)*100}")" "$(mem hsm-m-web)" | tee -a $R; }
sleep 30
win "idle (300s)" 300
timeout 70 curl -sN -H "Cookie: hsm_session=$COOKIE" "localhost:$PORT/api/live" >/dev/null & sleep 5
win "1 viewer (60s)" 60; wait
for _ in $(seq 10); do timeout 70 curl -sN -H "Cookie: hsm_session=$COOKIE" "localhost:$PORT/api/live" >/dev/null & done; sleep 5
win "10 viewers (60s)" 60; wait
echo "results in $R"
