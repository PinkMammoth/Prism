#!/usr/bin/env bash
# Deploy ONE committed Prism revision to the always-on Railway runtime (docs/OPERATIONS.md).
#   deploy/railway/deploy.sh [<commit-ish>]      default HEAD
# The upload is `git archive <commit>` + REVISION: never the working tree, never data/.env.
# Before replacing a running deployment it takes a verified pre-deploy backup on the server.
# Database migrations run on the first writable open (the boot catch-up cycle).
set -euo pipefail
REPO=$(git rev-parse --show-toplevel)
cd "$REPO"
REF=${1:-HEAD}
COMMIT=$(git rev-parse --verify "${REF}^{commit}")
SERVICE=${PRISM_RAILWAY_SERVICE:-prism-runtime}

if [ "$REF" = HEAD ] && ! git diff --quiet HEAD -- src config pyproject.toml uv.lock deploy; then
  echo "refusing: tracked changes are not committed (deploy an exact commit)" >&2
  exit 1
fi
if ! git branch -r --contains "$COMMIT" | grep -q .; then
  echo "warning: $COMMIT is not on any remote branch yet (push it so the revision is recoverable)" >&2
fi

echo "== current server state"
railway ssh --service "$SERVICE" -- market ops preflight || true
if railway ssh --service "$SERVICE" -- test -f /data/prism.duckdb; then
  echo "== pre-deploy backup"
  railway ssh --service "$SERVICE" -- market ops backup --kind manual --label "pre-deploy-${COMMIT:0:12}"
fi

OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
git archive "$COMMIT" | tar -x -C "$OUT"
echo "$COMMIT" > "$OUT/REVISION"
cp "$OUT/deploy/railway/railway.json" "$OUT/railway.json"
# Railway builds the root Dockerfile (RAILWAY_DOCKERFILE_PATH=Dockerfile), never Railpack autodetection.
cp "$OUT/deploy/railway/Dockerfile" "$OUT/Dockerfile"
echo "== deploying $COMMIT to $SERVICE"
railway up "$OUT" --path-as-root --service "$SERVICE" --ci --message "prism ${COMMIT:0:12}"
echo "== deployed. Next: watch the boot cycle and record the deployment:"
echo "   railway logs --service $SERVICE"
echo "   railway ssh --service $SERVICE -- market ops preflight"
echo "   railway ssh --service $SERVICE -- market ops record-deploy --note '<what changed>'"
echo "   railway ssh --service $SERVICE -- market status"
