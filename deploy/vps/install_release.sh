#!/usr/bin/env bash
# On the VPS, as root: install ONE committed release and switch to it.
#   install_release.sh <archive.tar.gz> <commit> [--restart-collector] [--no-restart]
# /opt/prism/releases/<commit> is immutable once installed; /opt/prism/current is an atomic
# symlink. Units are restarted individually; the microstructure collector keeps running (and
# its websocket) unless --restart-collector, because a restart loses its open minute.
set -euo pipefail
ARCHIVE=$1
REV=$2
shift 2
RESTART_COLLECTOR=0
RESTART=1
for a in "$@"; do
  case "$a" in
    --restart-collector) RESTART_COLLECTOR=1 ;;
    --no-restart) RESTART=0 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[[ "$REV" =~ ^[0-9a-f]{40}$ ]] || { echo "commit must be a full 40-hex revision" >&2; exit 2; }
DEST=/opt/prism/releases/$REV
as_prism() {  # run in the release with exactly the units' environment
  sudo -u prism env -i PATH="$DEST/.venv/bin:/usr/local/bin:/usr/bin:/bin" bash -c \
    'set -a; . /etc/prism/prism.env; [ -r /etc/prism/secrets.env ] && . /etc/prism/secrets.env
     set +a; export PRISM_HOME="$0"; exec "$@"' "$DEST" "$@"
}

# A release directory is complete only with its .installed marker (an interrupted install is
# rebuilt). The venv is built in place: its scripts carry absolute interpreter paths.
if [ ! -f "$DEST/.installed" ]; then
  rm -rf "$DEST"
  install -d -o prism -g prism "$DEST"
  tar -xzf "$ARCHIVE" -C "$DEST"
  echo "$REV" > "$DEST/REVISION"
  chown -R prism:prism "$DEST"
  (cd "$DEST" && sudo -u prism env UV_PYTHON_INSTALL_DIR=/opt/prism/python UV_PYTHON_DOWNLOADS=never \
     UV_PROJECT_ENVIRONMENT="$DEST/.venv" UV_CACHE_DIR=/var/lib/prism/.cache/uv \
     /usr/local/bin/uv sync --frozen --no-dev --python 3.12.3)
  touch "$DEST/.installed"
fi
[ "$(cat "$DEST/REVISION")" = "$REV" ] || { echo "release $DEST REVISION mismatch" >&2; exit 1; }

echo "== profile check (fail closed) with the new release"
as_prism "$DEST/.venv/bin/market" ops profile > /dev/null

PREV=$(readlink -f /opt/prism/current 2>/dev/null || true)
if [ -f /data/prism.duckdb ] && as_prism "$DEST/.venv/bin/market" ops profile --process scheduler 2>/dev/null; then
  echo "== pre-deploy backup (authoritative/rehearsal database only)"
  as_prism "$DEST/.venv/bin/market" ops backup --kind manual --label "pre-deploy-${REV:0:12}" || {
    echo "pre-deploy backup failed; release NOT switched" >&2; exit 1; }
fi

ln -sfn "$DEST" /opt/prism/current.new
mv -T /opt/prism/current.new /opt/prism/current
echo "$REV" > /opt/prism/CURRENT_REVISION
[ -n "$PREV" ] && [ "$PREV" != "$DEST" ] && basename "$PREV" > /opt/prism/PREVIOUS_REVISION

install -m 0644 "$DEST"/deploy/vps/systemd/*.service "$DEST"/deploy/vps/systemd/prism.target /etc/systemd/system/
systemctl daemon-reload
if [ "$RESTART" = 1 ]; then
  for u in prism-scheduler prism-nimble prism-universe prism-free-source-ingest prism-context-ingest \
           prism-context-gateway prism-free-sources; do
    systemctl try-restart "$u.service" || true
  done
  [ "$RESTART_COLLECTOR" = 1 ] && systemctl try-restart prism-microstructure.service
fi
# Keep the five newest releases plus current/previous; never delete one a process still uses.
ls -1dt /opt/prism/releases/*/ 2>/dev/null | tail -n +6 | while read -r old; do
  old=${old%/}
  case "$old" in "$DEST"|"$PREV") continue ;; esac
  if ! grep -qs "$old" /proc/[0-9]*/maps 2>/dev/null; then rm -rf "$old"; fi
done
echo "== installed $REV (previous: ${PREV:-none})"
systemctl --no-pager --plain list-units 'prism-*' || true
