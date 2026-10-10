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
# Independent watchdog catches a dead worker as well as a dead HTTP listener. No DB open.
if [ "${PRISM_CONTEXT_GATEWAY:-off}" = on ]; then
  echo '* * * * * market context gateway health --check --json' >> /tmp/prism.crontab
fi
if [ "${PRISM_FREE_SOURCES:-off}" = on ]; then
  echo '* * * * * market context sources status --check --json' >> /tmp/prism.crontab
fi
cat /tmp/prism.crontab
# Phase 24A: the persistent Hyperliquid microstructure collector (public market data only).
# It never opens the database (it appends to /data/microstructure; the scheduled
# `microstructure` job ingests). Supervised here: restarted 10 s after any exit. On container
# stop it is SIGKILLed with the container; the open minute is lost and recorded as PARTIAL/GAP.
if [ "${PRISM_RUNTIME_ROLE:-}" = authoritative ] && [ "${PRISM_MICROSTRUCTURE:-on}" != off ]; then
  mkdir -p /data/microstructure
  ( while true; do
      market microstructure collect || echo "microstructure collector exited ($?); restarting in 10s"
      sleep 10
    done ) &
fi
# Phase 26A: opt-in only. Both processes share /data; HTTP never opens DuckDB.
# Public Railway routing, OAuth, allowlists and TLS proxy trust are configured separately.
if [ "${PRISM_RUNTIME_ROLE:-}" = authoritative ] && [ "${PRISM_CONTEXT_GATEWAY:-off}" = on ]; then
  mkdir -p /data/context_gateway
  ( while true; do
      python -m market_signal.context.gateway.launch || echo "context gateway exited; restarting in 10s"
      sleep 10
    done ) &
  ( while true; do
      python -m market_signal.context.gateway.worker || echo "context ingest worker exited; restarting in 10s"
      sleep 10
    done ) &
fi
# Phase 29: opt-in zero-subscription public acquisition; no DB in the poller.
if [ "${PRISM_RUNTIME_ROLE:-}" = authoritative ] && [ "${PRISM_FREE_SOURCES:-off}" = on ]; then
  mkdir -p /data/free_event_sources
  ( while true; do
      python -m market_signal.context.free_sources.collector || echo "free source collector exited; restarting in 10s"
      sleep 10
    done ) &
  # Reuse the existing authoritative gateway worker when it is already running.
  if [ "${PRISM_CONTEXT_GATEWAY:-off}" != on ]; then
    ( while true; do
        python -m market_signal.context.free_sources.worker || echo "free source ingest exited; restarting in 10s"
        sleep 10
      done ) &
  fi
fi
# Phase 27: deterministic PAPER worker, idle until a separate run is registered/activated.
# It releases DuckDB/runtime locks between ticks; quotes come from the existing public collector.
if [ "${PRISM_RUNTIME_ROLE:-}" = authoritative ] && [ "${PRISM_NIMBLE_RUNTIME:-on}" != off ]; then
  ( while true; do
      python -m market_signal.paper.nimble.worker || echo "nimble paper worker exited; restarting in 10s"
      sleep 10
    done ) &
fi
# Restart/reboot/deploy catch-up: one idempotent prospective cycle (a no-op if nothing is new).
( sleep "${PRISM_BOOT_DELAY:-60}"; market ops cycle prospective --trigger boot --wait 3600 || true ) &
exec supercronic -passthrough-logs /tmp/prism.crontab
