#!/usr/bin/env bash
# Home PC (read-only role): copy the newest verified production backup down for inspection.
#   deploy/railway/pull_backup.sh            -> data/remote/prism-<stamp>.duckdb
# Production is never written. The copy keeps the server's authority claim, so on this
# machine it opens read-only (`market --db data/remote/<file> status`); writes are refused
# unless you deliberately set PRISM_RUNTIME_ROLE=scratch for experiments on the copy.
set -euo pipefail
REPO=$(git rev-parse --show-toplevel)
cd "$REPO"
SERVICE=${PRISM_RAILWAY_SERVICE:-prism-runtime}
VOLUME=${PRISM_RAILWAY_VOLUME:-prism-runtime-volume}
KIND=${1:-daily}
LATEST=$(railway ssh --service "$SERVICE" -- sh -c "ls -1 /data/backups/$KIND/prism-*.duckdb | tail -n 1" | tr -d '\r')
[ -n "$LATEST" ] || { echo "no $KIND backup on the server" >&2; exit 1; }
mkdir -p data/remote
DEST="data/remote/$(basename "$LATEST")"
railway volume files --volume "$VOLUME" download "${LATEST#/data}" "$DEST"
REMOTE_SHA=$(railway ssh --service "$SERVICE" -- sha256sum "$LATEST" | awk '{print $1}')
LOCAL_SHA=$(sha256sum "$DEST" | awk '{print $1}')
[ "$REMOTE_SHA" = "$LOCAL_SHA" ] || { echo "hash mismatch for $DEST" >&2; exit 1; }
uv run market ops verify-backup "$DEST" >/dev/null
echo "$DEST (sha256 $LOCAL_SHA) verified; inspect with: uv run market --db $DEST status"
