#!/bin/sh
# Separate maintenance lifecycle. Existing owner flock; no collector restart or DB writer.
set -eu
cd "${PRISM_RETENTION_HOME:?}"
case "${PRISM_RETENTION_ACTION:?}" in
  report)
    exec "$PRISM_RETENTION_HOME/.venv/bin/python" -m market_signal.ops.retention_phase1 \
      report --db "${PRISM_RETENTION_DB:?}" --raw "${PRISM_RETENTION_RAW:?}" --summary
    ;;
  apply)
    exec "$PRISM_RETENTION_HOME/.venv/bin/python" -m market_signal.ops.retention_phase1 \
      apply --db "${PRISM_RETENTION_DB:?}" --raw "${PRISM_RETENTION_RAW:?}" \
      --family hyperliquid_settled_funding --max-files 100 --summary
    ;;
  *) exit 78 ;;
esac
