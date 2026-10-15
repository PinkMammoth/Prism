#!/usr/bin/env bash
# Root. Stage a new Research release beside the current one and switch only the Research
# symlink. Research has no installed processes, so this activates nothing; Lab's
# /opt/prism/current is never touched. Rollback: repoint /opt/prism/research/current.
# Usage: stage_research_release.sh ARCHIVE REVISION
set -euo pipefail
[ "$(id -u)" = 0 ]
ARCHIVE=$1
REV=$2
[[ "$REV" =~ ^[0-9a-f]{40}$ ]]
DEST=/opt/prism/research/releases/$REV
[ ! -e "$DEST" ]
[ -z "$(systemctl list-units 'prism-research*' --no-legend --plain | grep -v '^prism-research-backup')" ]
install -d -m 0755 "$DEST"
tar -xzf "$ARCHIVE" -C "$DEST"
printf '%s\n' "$REV" > "$DEST/REVISION"
UV_PYTHON_INSTALL_DIR=/opt/prism/python UV_PYTHON_DOWNLOADS=never \
  UV_PROJECT_ENVIRONMENT="$DEST/.venv" UV_CACHE_DIR=/var/lib/prism-research/.uv-cache \
  /usr/local/bin/uv sync --project "$DEST" --frozen --no-dev --python 3.12.3
chown -R root:root "$DEST"
chmod -R go-w "$DEST"
PREVIOUS=$(readlink /opt/prism/research/current)
ln -s "$DEST" /opt/prism/research/current.new
mv -T /opt/prism/research/current.new /opt/prism/research/current
echo "research current: $PREVIOUS -> $DEST"
