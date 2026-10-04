#!/usr/bin/env bash
# Prism Strategy Lab forward tracking: perp data update -> prospective check -> resolve.
# Idempotent; meant to be triggered several times a day by Windows Task Scheduler (see
# docs/STRATEGY_LAB.md section 13). Not installed anywhere automatically.
REPO=/home/matth/prism
UV=/home/matth/.local/bin/uv
LOG=data/forward.log

cd "$REPO" || exit 1

# Skip if a previous forward run is still going. Overlap with daily.sh / oi_collect.sh is
# handled by Prism's database lock (the later process waits, up to PRISM_LOCK_TIMEOUT).
exec 9>data/.forward.lock
if ! flock -n 9; then
  echo "=== $(date -Iseconds) skipped: already running" >> "$LOG"
  exit 0
fi

{
  echo "=== $(date -Iseconds) start"
  "$UV" run market lab forward run;  rc=$?
  echo "=== done forward=$rc"
} >> "$LOG" 2>&1

# Keep the log to the last 5000 lines.
if [ "$(wc -l < "$LOG")" -gt 5000 ]; then
  tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit "$rc"
