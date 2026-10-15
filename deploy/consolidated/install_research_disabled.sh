#!/usr/bin/env bash
# Dedicated disabled namespace. Never calls the singleton Lab installer/systemctl.
# Usage (root): install_research_disabled.sh archive revision overlay_sha256
set -euo pipefail
[ "$(id -u)" = 0 ]
ARCHIVE=$1
REV=$2
OVERLAY=$3
[[ "$REV" =~ ^[0-9a-f]{40}$ ]]
[[ "$OVERLAY" =~ ^[0-9a-f]{64}$ ]]
ROOT=/var/lib/prism-research
DEST=/opt/prism/research/releases/$REV-${OVERLAY:0:12}
# Require a fresh installation; a retry must inspect the partial state first.
[ ! -e /opt/prism/research/current ]
[ ! -e "$ROOT/import/prism.duckdb" ]
if ! getent passwd prism-research >/dev/null; then
  useradd --system --home-dir "$ROOT" --shell /usr/sbin/nologin prism-research
fi
[ "$(id -G prism-research)" = "$(id -g prism-research)" ]
install -d -m 0750 -o root -g prism-research "$ROOT" "$ROOT/import" /etc/prism-research
for name in backups logs state scratch results raw microstructure context_gateway free_event_sources; do
  install -d -m 0700 -o prism-research -g prism-research "$ROOT/$name"
done
install -d -m 0755 "$DEST"
tar -xzf "$ARCHIVE" -C "$DEST"
printf '%s\n' "$REV" > "$DEST/REVISION"
printf '%s\n' "$OVERLAY" > "$DEST/NAMESPACE_OVERLAY_SHA256"
# A separate venv/cache; shared pinned Python is read only. No Lab dependency changes.
UV_PYTHON_INSTALL_DIR=/opt/prism/python UV_PYTHON_DOWNLOADS=never \
  UV_PROJECT_ENVIRONMENT="$DEST/.venv" UV_CACHE_DIR="$ROOT/.uv-cache" \
  /usr/local/bin/uv sync --project "$DEST" --frozen --no-dev --python 3.12.3
chown -R root:root "$DEST"
chmod -R go-w "$DEST"
install -m 0640 -o root -g prism-research "$DEST/deploy/consolidated/research.env.example" /etc/prism-research/runtime.env
install -m 0640 -o root -g prism-research /dev/null /etc/prism-research/secrets.env
install -d -m 0750 -o root -g prism-research /etc/prism-research/prepared-services
cp "$DEST"/deploy/consolidated/prism-research* /etc/prism-research/prepared-services/
ln -s "$DEST" /opt/prism/research/current
# No unit files in /etc/systemd/system; no reload/enable/start; no scheduler file.
sudo -u prism-research env -i PATH="$DEST/.venv/bin:/usr/bin:/bin" bash -c \
  'set -a; . /etc/prism-research/runtime.env; set +a; market ops profile' \
  > "$ROOT/state/profile.json"
chown root:prism-research "$ROOT/state/profile.json"
chmod 0640 "$ROOT/state/profile.json"
