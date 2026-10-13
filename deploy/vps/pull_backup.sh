#!/usr/bin/env bash
# From the archive machine: copy the VPS's newest verified recovery backup off-host, check its
# checksum against the server-side manifest, verify it locally and restore-test it.
#   PRISM_VPS_HOST=user@host deploy/vps/pull_backup.sh [<destination-dir>]
# Never deletes anything on either side. Uses SSH only (provider-agnostic).
set -euo pipefail
REPO=$(git rev-parse --show-toplevel)
cd "$REPO"
HOST=${PRISM_VPS_HOST:?set PRISM_VPS_HOST=user@host}
DEST=${1:-data/backup-archive/vps}
mkdir -p "$DEST"
INV=$(ssh "$HOST" "sudo -u prism bash -c 'set -a; . /etc/prism/prism.env; set +a; \
  /opt/prism/current/.venv/bin/market ops backup-inventory'")
read -r SRC SHA < <(printf '%s' "$INV" | python3 -c '
import json, sys
rows = json.load(sys.stdin)
if not rows:
    sys.exit("no backup on the server")
r = rows[-1]
print(r["path"], r["manifest"].get("sha256", ""))')
[ -n "$SHA" ] || { echo "server backup has no manifest checksum; refusing" >&2; exit 1; }
NAME=$(basename "$SRC")
[ -e "$DEST/$NAME.gz" ] && { echo "already archived: $DEST/$NAME.gz"; exit 0; }
rsync --partial --times "$HOST:$SRC" "$DEST/$NAME.part"
echo "$SHA  $DEST/$NAME.part" | sha256sum -c -
mv "$DEST/$NAME.part" "$DEST/$NAME"
export PRISM_PROFILE=autotrader_lab PRISM_DEPLOYMENT_TARGET=local PRISM_RUNTIME_ROLE=scratch
uv run --frozen market ops verify-backup "$DEST/$NAME"
uv run --frozen market ops restore-test "$DEST/$NAME"
gzip -6 "$DEST/$NAME"
sha256sum "$DEST/$NAME.gz" > "$DEST/$NAME.gz.sha256"
echo "archived $DEST/$NAME.gz (source sha256 $SHA)"
