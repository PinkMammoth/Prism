#!/usr/bin/env bash
# Root, on the OVH host. Installs the isolated Research backup identity, pinned tools and
# prepared (not enabled) units. Never touches Lab units/config/DB or Research authority.
# Usage: install_research_backup.sh TOOLS_DIR SOURCE_DESCRIPTOR_JSON
#   TOOLS_DIR holds the signature-verified restic/rclone binaries checked below.
set -euo pipefail
[ "$(id -u)" = 0 ]
TOOLS=$1
SOURCE=$2
RESTIC_SHA=20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639  # restic 0.19.1
RCLONE_SHA=f66d8c1d552ad90296a11bc8b46d56a7fa5da1a7fa05e7ca522d95df92c4a4c0  # rclone v1.75.1
echo "$RESTIC_SHA  $TOOLS/restic" | sha256sum -c --quiet
echo "$RCLONE_SHA  $TOOLS/rclone" | sha256sum -c --quiet
U=prism-research-backup
HOME_DIR=/var/lib/prism-research-backup
ETC=/etc/prism-research-backup
REL=/opt/prism/research/current/deploy/consolidated
if ! getent passwd "$U" >/dev/null; then
  useradd --system --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$U"
fi
# Read access to the sealed Research DB only; no Lab group.
usermod -a -G prism-research "$U"
# Exactly its own group plus prism-research (never a Lab group).
[ "$(id -nG "$U" | tr ' ' '\n' | sort | tr '\n' ' ')" = "prism-research prism-research-backup " ]
install -d -m 0755 -o root -g root /opt/prism/research-backup /opt/prism/research-backup/tools
install -d -m 0755 -o root -g root /opt/prism/research-backup/tools/bin
install -m 0755 -o root -g root "$TOOLS/restic" "$TOOLS/rclone" /opt/prism/research-backup/tools/bin/
install -d -m 0700 -o "$U" -g "$U" "$HOME_DIR" "$HOME_DIR/generations" "$HOME_DIR/scratch" "$HOME_DIR/state" "$ETC"
[ -e "$ETC/backup.env" ] || install -m 0600 -o "$U" -g "$U" "$REL/research-backup.env.example" "$ETC/backup.env"
[ -e "$ETC/backup.json" ] || install -m 0600 -o "$U" -g "$U" "$REL/research-backup.json.example" "$ETC/backup.json"
install -m 0600 -o "$U" -g "$U" "$SOURCE" "$ETC/source.json"
# Machine repository key: generated here, never printed. The user's independent recovery
# key is added separately (research_backup_recovery_key.sh) so losing this file is survivable.
if [ ! -e "$ETC/restic-password" ]; then
  (umask 077; head -c 48 /dev/urandom | base64 -w0 > "$ETC/restic-password.tmp")
  chown "$U:$U" "$ETC/restic-password.tmp"; chmod 0400 "$ETC/restic-password.tmp"
  mv "$ETC/restic-password.tmp" "$ETC/restic-password"
fi
install -m 0644 -o root -g root "$REL/prism-research-backup.service" "$REL/prism-research-backup.timer" /etc/systemd/system/
systemctl daemon-reload
# Timer is enabled only after Drive authorization, repository init and restore proof.
