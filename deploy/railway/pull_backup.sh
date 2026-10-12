#!/usr/bin/env bash
# Export latest existing Railway recovery backup to LOCAL storage, checksum and restore-test.
# Optional destination directory (external storage is supported). Never deletes by default.
set -euo pipefail
REPO=$(git rev-parse --show-toplevel)
cd "$REPO"
DESTINATION=${1:-data/backup-archive}
exec uv run python scripts/backup_archive.py "$DESTINATION" \
  --service "${PRISM_RAILWAY_SERVICE:-prism-runtime}"
