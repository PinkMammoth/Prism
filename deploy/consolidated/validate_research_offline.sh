#!/usr/bin/env bash
# One-shot offline read validation only. Never starts any service or claims a DB.
set -euo pipefail
set -a
. /etc/prism-research/runtime.env
set +a
[ "$PRISM_PROFILE" = research_vps ]
[ "$PRISM_AUTHORITY" = none ]
[ "$PRISM_RUNTIME_ROLE" = reader ]
[ -z "$PRISM_RUNTIME_ID" ]
[ "$PRISM_MICROSTRUCTURE" = off ]
[ "$PRISM_CONTEXT_GATEWAY" = off ]
[ "$PRISM_FREE_SOURCES" = off ]
cd "$PRISM_HOME"
exec "$PRISM_HOME/.venv/bin/python" "$PRISM_HOME/scripts/ovh_research_validate.py" "$@"
