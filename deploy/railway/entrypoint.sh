#!/bin/sh
# Prism runtime container entrypoint (PID 1 is tini, which reaps the catch-up child).
# It never creates or modifies the database by itself: every job refuses until the volume
# holds a database whose authority claim names this runtime (PRISM_RUNTIME_ID).
set -eu
cd /app
echo "prism runtime: revision $(cat REVISION) role=${PRISM_RUNTIME_ROLE:-unset} id=${PRISM_RUNTIME_ID:-unset}"
mkdir -p /data/logs /data/backups
market ops preflight || echo "preflight: not ready for jobs (expected only before the database is installed)"
market ops crontab > /tmp/prism.crontab
cat /tmp/prism.crontab
# Restart/reboot/deploy catch-up: one idempotent prospective cycle (a no-op if nothing is new).
( sleep "${PRISM_BOOT_DELAY:-60}"; market ops cycle prospective --trigger boot --wait 3600 || true ) &
exec supercronic -passthrough-logs /tmp/prism.crontab
