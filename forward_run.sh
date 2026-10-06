#!/usr/bin/env bash
# Prism Strategy Lab forward tracking: perp data update -> prospective check -> resolve,
# then the perps co-pilot (a separate consumer: reads Lab evidence, may send Telegram
# alerts, never writes research records), then the PAPER auto-trader (another separate
# consumer: a simulated account that can never place a real order; writes only paper_*
# tables), then the PAPER daily brief (observability only). Idempotent; meant to be
# triggered several times a day by Windows Task Scheduler (see docs/STRATEGY_LAB.md
# sections 13, 15, 17 and 18). Not installed anywhere automatically.
# Phase 14: production runs on the always-on Railway runtime (docs/OPERATIONS.md). This home-PC
# wrapper is kept only for rollback; its task is disabled, and against the claimed live database
# every write below is refused unless this machine is made authoritative again.
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
  # Data was just refreshed above; the co-pilot never changes forward evidence.
  "$UV" run market lab copilot run --no-update;  cop=$?
  # Paper account: independent of the co-pilot's alerts and of Telegram delivery.
  "$UV" run market lab paper run;  pap=$?
  # After the paper cycle committed: immutable snapshot -> one stored brief per completed paper
  # day -> Telegram (once). Remove --send to keep the brief CLI-only. Never affects trading.
  "$UV" run market lab paper brief --send;  brf=$?
  echo "=== done forward=$rc copilot=$cop paper=$pap brief=$brf"
} >> "$LOG" 2>&1

# Keep the log to the last 5000 lines.
if [ "$(wc -l < "$LOG")" -gt 5000 ]; then
  tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

[ "$rc" -ne 0 ] && exit "$rc"
[ "$cop" -ne 0 ] && exit "$cop"
[ "$pap" -ne 0 ] && exit "$pap"
exit "$brf"
