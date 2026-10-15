#!/usr/bin/env bash
# Root, interactive (ssh -t). Adds the custodian's independent restic recovery key and
# proves it opens the repository WITHOUT the machine key. Passwords are typed at the
# terminal prompt only (restic reads the tty; nothing echoed, logged or passed in argv).
set -euo pipefail
[ "$(id -u)" = 0 ]
[ -t 0 ] || { echo "run with ssh -t so restic can prompt" >&2; exit 1; }
U=prism-research-backup
run() {
  sudo -u "$U" env -i HOME=/var/lib/prism-research-backup PATH=/opt/prism/research-backup/tools/bin:/usr/bin:/bin \
    bash -c 'set -a; . /etc/prism-research-backup/backup.env; set +a; '"$1"
}
echo "Step 1/2: choose a NEW recovery password (store it in your password manager first)."
run 'restic --no-cache key add --host custodian-recovery --user prism-owner'
echo "Step 2/2: type the SAME recovery password again to prove it works without the VPS key."
run 'unset RESTIC_PASSWORD_FILE; restic --no-cache cat config >/dev/null && echo "recovery key verified: repository opened without the VPS key"'
run 'restic --no-cache key list --json' | python3 -I -c '
import json, sys
keys = json.load(sys.stdin)
print("repository keys:", len(keys), sorted(k["hostName"] for k in keys))
'
