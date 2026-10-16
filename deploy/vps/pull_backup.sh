#!/usr/bin/env bash
# Pull the latest registered Lab backup, checksum/restore-test, archive and acknowledge it.
# Retention is still governed by backup-retain/the scheduler; this helper deletes no backup.
#   PRISM_VPS_HOST=user@host deploy/vps/pull_backup.sh [<destination-dir>] [--source PATH]
set -euo pipefail
REPO=$(git rev-parse --show-toplevel)
cd "$REPO"
exec uv run --frozen python scripts/vps_backup_archive.py "$@"
