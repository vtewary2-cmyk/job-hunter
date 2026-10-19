#!/bin/bash
# 9:00 dispatch: make sure the Job Hunter Chrome profile is open and drivable,
# then run the daily pipeline (Lead CSM + Product Consultant roles).
#
# Run by launchd (com.jobhunter.morning). The laptop is switched on at 8:55.
set -uo pipefail

JH_HOME="${JOBHUNTER_HOME:-$HOME/JobHunter}"
APP="$JH_HOME/app"
PROFILE="$JH_HOME/chrome"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP="http://127.0.0.1:9222"

log() { printf '%s morning: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Candidate asked for a break: no runs before this date (YYYY-MM-DD).
PAUSE_UNTIL="2026-10-12"  # set a later date to pause scheduled runs
if [[ "$(date +%F)" < "$PAUSE_UNTIL" ]]; then
  log "paused until $PAUSE_UNTIL at the candidate's request — skipping this run"
  exit 0
fi

# Never run two at once (they overwrite each other's answers): wait up to 2h for a run in progress.
for _ in $(seq 1 240); do
  pgrep -f "^/usr/bin/caffeinate -dimsu" >/dev/null || break
  [ "${waited:-}" ] || { log "another run is still going — waiting for it to finish"; waited=1; }
  sleep 30
done

cdp_up() { curl -s -m 3 -o /dev/null "$CDP/json/version"; }

page_count() {
  curl -s -m 5 "$CDP/json/list" | "$APP/.venv/bin/python" -I -c \
    "import json,sys; print(sum(t.get('type')=='page' for t in json.load(sys.stdin)))" 2>/dev/null || echo 0
}

# 1. Is the Job Hunter Chrome profile open with its debugging port?
if cdp_up; then
  log "Job Hunter Chrome already open"
elif pgrep -f "user-data-dir=$PROFILE" >/dev/null; then
  # Open but without the port: it can't be driven, so restart it with the port.
  log "Job Hunter Chrome open without debugging port — restarting it"
  pkill -f "user-data-dir=$PROFILE"
  sleep 5
fi

if ! cdp_up; then
  log "opening the Job Hunter Chrome profile"
  # Through LaunchServices, not as a child: launchd kills a job's children
  # when the job exits, which would close the window after every run.
  open -gna "Google Chrome" --args --user-data-dir="$PROFILE" --remote-debugging-port=9222 \
    --remote-debugging-address=127.0.0.1 --no-first-run --no-default-browser-check \
    --window-position=60,60 --window-size=1280,860 \
    "https://www.linkedin.com/feed/"
  for _ in $(seq 1 30); do cdp_up && break; sleep 2; done
fi

if ! cdp_up; then
  log "ERROR: could not open the Job Hunter Chrome profile — giving up"
  exit 1
fi

# 2. Attaching fails when the window has no tabs, so make sure one exists.
if [ "$(page_count)" -eq 0 ]; then
  log "no tabs open — opening LinkedIn"
  curl -s -m 10 -X PUT "$CDP/json/new?https://www.linkedin.com/feed/" >/dev/null
fi
sleep 8

# 3. Apply: newest postings first, until settings' per-run target (scripts/apply_run.py).
# Daily schedule (candidate, 2026-10-14): 8:55 → 5-10 (aim 10), 19:00 → 10, 23:00 → 2-3 (aim 3).
case "$(date +%H)" in
  08|09|10|11) export APPLY_TARGET=10 ;;
  17|18|19|20|21) export APPLY_TARGET=10 ;;
  22|23|00|01) export APPLY_TARGET=3 ;;
  *) export APPLY_TARGET=5 ;;
esac
log "starting the application run (target $APPLY_TARGET)"
# Keep the window minimized so runs never pop up over whatever is on screen.
( while sleep 15; do "$APP/.venv/bin/python" "$APP/scripts/minimize_chrome.py" >/dev/null 2>&1; done ) &
MINIMIZER=$!
trap 'kill $MINIMIZER 2>/dev/null' EXIT
# Keep the Mac awake until the run ends (sleep stalled a run for hours on 2026-10-12).
cd "$APP" && JOBHUNTER_HOME="$JH_HOME" /usr/bin/caffeinate -dimsu "$APP/.venv/bin/python" scripts/apply_run.py
code=$?
log "run finished with exit code $code"
exit $code
