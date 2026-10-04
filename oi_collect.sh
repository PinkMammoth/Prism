#!/usr/bin/env bash
# Prism OI collection: Hyperliquid OI snapshot + Binance OI rolling backfill (data only).
# Meant to be triggered several times a day by Windows Task Scheduler (see
# docs/OPEN_INTEREST.md).
# Phase 14: production runs on the always-on Railway runtime (docs/OPERATIONS.md). This home-PC
# wrapper is kept only for rollback; its task is disabled, and against the claimed live database
# every write below is refused unless this machine is made authoritative again.
REPO=/home/matth/prism
UV=/home/matth/.local/bin/uv
LOG=data/oi.log

cd "$REPO" || exit 1

# Skip if a previous OI run is still going. Overlap with daily.sh is handled by Prism's
# database lock (the later process waits, up to PRISM_LOCK_TIMEOUT seconds).
exec 9>data/.oi.lock
if ! flock -n 9; then
  echo "=== $(date -Iseconds) skipped: already running" >> "$LOG"
  exit 0
fi

{
  echo "=== $(date -Iseconds) start"
  "$UV" run market oi collect;  rc=$?
  echo "=== done oi=$rc"
} >> "$LOG" 2>&1

# Keep the log to the last 5000 lines.
if [ "$(wc -l < "$LOG")" -gt 5000 ]; then
  tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit "$rc"
