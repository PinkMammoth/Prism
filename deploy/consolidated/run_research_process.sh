#!/bin/sh
# Prepared namespaced runner; no absolute /data lab paths, no runtime installation.
set -eu
cd "${PRISM_HOME:?}"
market ops profile --process "${1:?}" > /dev/null
case "$1" in
  microstructure_collector) exec market microstructure collect ;;
  context_gateway) exec python -m market_signal.context.gateway.launch ;;
  context_ingest) exec python -m market_signal.context.gateway.worker ;;
  free_source_collector) exec python -m market_signal.context.free_sources.collector ;;
  scheduler)
    mkdir -p "${PRISM_STATE_DIR:?}" "${PRISM_LOG_DIR:?}" "${PRISM_BACKUP_DIR:?}"
    market ops profile > "$PRISM_STATE_DIR/profile.json"
    # Preflight must succeed; do not open/migrate/claim automatically to make it pass.
    market ops preflight > "$PRISM_STATE_DIR/preflight.json"
    market ops crontab > "$PRISM_STATE_DIR/prism.crontab.tmp"
    mv "$PRISM_STATE_DIR/prism.crontab.tmp" "$PRISM_STATE_DIR/prism.crontab"
    # Catch-up is an explicit controlled cutover step, not a background authority race.
    exec supercronic -passthrough-logs "$PRISM_STATE_DIR/prism.crontab"
    ;;
  *) exit 78 ;;
esac
