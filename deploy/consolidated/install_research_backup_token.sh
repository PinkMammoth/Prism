#!/usr/bin/env bash
# Root. Reads `rclone authorize` output on STDIN (never argv/terminal) and writes the
# drive.file-scoped remote into the backup identity's private rclone.conf. Prints nothing secret.
set -euo pipefail
[ "$(id -u)" = 0 ]
U=prism-research-backup
CONF=/etc/prism-research-backup/rclone.conf
[ ! -e "$CONF" ] || { echo "rclone.conf exists; rotate by moving it aside first" >&2; exit 1; }
umask 077
python3 -I -c '
import json, re, sys
text = sys.stdin.read()
match = re.search(r"--->\s*(\{.*\})\s*<---", text, re.S)
token = json.loads(match.group(1) if match else text.strip())
assert token.get("refresh_token") and token.get("token_type"), "not an rclone OAuth token"
sys.stdout.write("[prism-gdrive]\ntype = drive\nscope = drive.file\nuse_trash = false\n"
                 "token = " + json.dumps(token, separators=(",", ":")) + "\n")
' > "$CONF.tmp"
chown "$U:$U" "$CONF.tmp"
chmod 0600 "$CONF.tmp"
mv "$CONF.tmp" "$CONF"
echo "installed $(stat -c '%U:%G %a' "$CONF") $CONF"
