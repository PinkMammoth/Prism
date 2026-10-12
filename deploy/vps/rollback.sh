#!/usr/bin/env bash
# On the VPS, as root: switch back to the previous (or a named, still-installed) release.
#   rollback.sh [<commit>] [--restart-collector]
# Code rollback only: the database stays. Migrations are forward-only, so a rollback across
# a schema migration restores the pre-deploy backup instead (docs/RUNTIME_PROFILE_SPLIT_AND_VPS_LAB.md).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
REV=$(cat /opt/prism/PREVIOUS_REVISION)
OPTS=()
for a in "$@"; do
  case "$a" in --*) OPTS+=("$a") ;; *) REV=$a ;; esac
done
DEST=/opt/prism/releases/$REV
[ -f "$DEST/.installed" ] || { echo "release $REV is not installed" >&2; exit 1; }
# an installed release needs no archive
exec "$DEST/deploy/vps/install_release.sh" /dev/null "$REV" "${OPTS[@]}"
