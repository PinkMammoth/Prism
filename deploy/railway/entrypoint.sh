#!/bin/sh
# Prism runtime container entrypoint (PID 1 is tini, which reaps the catch-up child).
# It never creates or modifies the database by itself: every job refuses until the volume
# holds a database whose authority claim names this runtime (PRISM_RUNTIME_ID).
# Which long-running processes start is decided by the versioned runtime profile
# (`market ops profile`; docs/RUNTIME_PROFILE_SPLIT_AND_VPS_LAB.md). Without PRISM_PROFILE the
# profile is `legacy`, which reproduces the pre-split flags exactly (PRISM_MICROSTRUCTURE and
# PRISM_NIMBLE_RUNTIME default on, PRISM_CONTEXT_GATEWAY and PRISM_FREE_SOURCES default off).
set -eu
cd /app
echo "prism runtime: revision $(cat REVISION) role=${PRISM_RUNTIME_ROLE:-unset} id=${PRISM_RUNTIME_ID:-unset} profile=${PRISM_PROFILE:-legacy}"
mkdir -p /data/logs /data/backups
# Fail closed: an invalid or contradictory profile starts nothing (exit 78).
PROCS=$(market ops profile --shell)
eval "$PROCS"
market ops profile
market ops preflight || echo "preflight: not ready for jobs (expected only before the database is installed)"
# The crontab holds only this profile's jobs plus the watchdogs of the processes it runs.
market ops crontab > /tmp/prism.crontab
cat /tmp/prism.crontab

supervise() {  # name, command...: restarted 10 s after any exit
  name=$1
  shift
  ( while true; do
      "$@" || echo "$name exited ($?); restarting in 10s"
      sleep 10
    done ) &
}
# Dynamic universe Stage A is opt-in (legacy PRISM_UNIVERSE_STAGE=ranking|warming; every
# explicit profile keeps it gated); network fetches stay outside paper writer locks.
if [ "$PRISM_PROC_universe_radar" = 1 ]; then
  supervise "universe radar" python -m market_signal.universe.worker
fi

# Phase 24A: the persistent Hyperliquid microstructure collector (public market data only).
# It never opens the database (it appends to /data/microstructure; the scheduled
# `microstructure` job ingests). On container stop it is SIGKILLed with the container; the
# open minute is lost and recorded as PARTIAL/GAP.
if [ "$PRISM_PROC_microstructure_collector" = 1 ]; then
  mkdir -p /data/microstructure
  if [ "$PRISM_COLLECTOR_DYNAMIC" = 1 ]; then
    supervise "microstructure collector" market microstructure collect --dynamic-universe
  else
    supervise "microstructure collector" market microstructure collect
  fi
fi
# Phase 26A: both processes share /data; HTTP never opens DuckDB.
# Public Railway routing, OAuth, allowlists and TLS proxy trust are configured separately.
if [ "$PRISM_PROC_context_gateway" = 1 ] || [ "$PRISM_PROC_context_ingest" = 1 ]; then
  mkdir -p /data/context_gateway
fi
if [ "$PRISM_PROC_context_gateway" = 1 ]; then
  supervise "context gateway" python -m market_signal.context.gateway.launch
fi
if [ "$PRISM_PROC_context_ingest" = 1 ]; then
  supervise "context ingest worker" python -m market_signal.context.gateway.worker
fi
# Phase 29: zero-subscription public acquisition; no DB in the poller. The gateway ingest
# worker drains this spool when it runs; otherwise the dedicated free-source ingest does.
if [ "$PRISM_PROC_free_source_collector" = 1 ]; then
  mkdir -p /data/free_event_sources
  supervise "free source collector" python -m market_signal.context.free_sources.collector
fi
if [ "$PRISM_PROC_free_source_ingest" = 1 ]; then
  supervise "free source ingest" python -m market_signal.context.free_sources.worker
fi
# Phase 27: deterministic PAPER worker, idle until a separate run is registered/activated.
# It releases DuckDB/runtime locks between ticks; quotes come from the existing public collector.
if [ "$PRISM_PROC_nimble_worker" = 1 ]; then
  supervise "nimble paper worker" python -m market_signal.paper.nimble.worker
fi
# Restart/reboot/deploy catch-up: one idempotent cycle of the profile's catch-up job.
if [ -n "$PRISM_BOOT_CATCHUP_JOB" ]; then
  ( sleep "${PRISM_BOOT_DELAY:-60}"; market ops boot-catchup --wait 3600 || true ) &
fi
exec supercronic -passthrough-logs /tmp/prism.crontab
