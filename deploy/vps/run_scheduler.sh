#!/bin/sh
# prism-scheduler.service main process: the host's ONLY scheduling authority (supercronic; no
# systemd timers). The crontab holds exactly the resolved profile's jobs and watchdogs.
set -eu
STATE=${PRISM_STATE_DIR:-/data/state}
mkdir -p "$STATE" /data/logs /data/backups
market ops profile > "$STATE/profile.json"   # fail closed: an invalid profile exits 78
market ops preflight || echo "preflight: not ready for jobs (expected before a database is installed)"
market ops crontab > "$STATE/prism.crontab.tmp"
mv "$STATE/prism.crontab.tmp" "$STATE/prism.crontab"
cat "$STATE/prism.crontab"
JOB=$(market ops profile --shell | sed -n 's/^PRISM_BOOT_CATCHUP_JOB=//p')
if [ -n "$JOB" ]; then
  # restart/reboot/deploy catch-up: one idempotent cycle (a no-op if nothing is new)
  ( sleep "${PRISM_BOOT_DELAY:-60}"; market ops boot-catchup --wait 3600 || true ) &
fi
exec supercronic -passthrough-logs "$STATE/prism.crontab"
