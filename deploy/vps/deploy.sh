#!/usr/bin/env bash
# From a development machine: deploy ONE committed Prism revision to a VPS.
#   PRISM_VPS_HOST=user@host deploy/vps/deploy.sh [<commit-ish>] [--restart-collector]
# The upload is `git archive <commit>` + REVISION, never the working tree, data or secrets.
# The release's own install_release.sh installs it (pre-deploy backup, atomic switch).
set -euo pipefail
REPO=$(git rev-parse --show-toplevel)
cd "$REPO"
HOST=${PRISM_VPS_HOST:?set PRISM_VPS_HOST=user@host}
REF=${1:-HEAD}
shift || true
COMMIT=$(git rev-parse --verify "${REF}^{commit}")
if [ "$REF" = HEAD ] && ! git diff --quiet HEAD -- src config pyproject.toml uv.lock deploy; then
  echo "refusing: tracked changes are not committed (deploy an exact commit)" >&2
  exit 1
fi
git branch -r --contains "$COMMIT" | grep -q . || \
  echo "warning: $COMMIT is not on any remote branch yet (push it so the revision is recoverable)" >&2
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
git archive --format=tar.gz -o "$OUT/release.tgz" "$COMMIT"
REMOTE=/tmp/prism-$COMMIT
ssh "$HOST" "mkdir -p $REMOTE"
scp -q "$OUT/release.tgz" "$HOST:$REMOTE/release.tgz"
ssh "$HOST" "cd $REMOTE && tar -xzf release.tgz deploy/vps/install_release.sh && \
  sudo bash deploy/vps/install_release.sh $REMOTE/release.tgz $COMMIT $* && rm -rf $REMOTE"
echo "== deployed $COMMIT to $HOST. Check: ssh $HOST 'sudo -u prism /opt/prism/current/.venv/bin/market ops profile'"
