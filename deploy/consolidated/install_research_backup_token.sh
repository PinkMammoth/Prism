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
import base64, json, re, sys
text = sys.stdin.read()
match = re.search(r"--->\s*(.*?)\s*<---", text, re.S)
blob = (match.group(1) if match else text).strip()
# rclone >= 1.6x emits base64 JSON {client_id, client_secret, token}; older emits token JSON.
data = json.loads(blob) if blob.startswith("{") else json.loads(base64.b64decode(blob + "=="))
extra = {k: data[k] for k in ("client_id", "client_secret") if data.get(k)}
token = data.get("token", data)
token = json.loads(token) if isinstance(token, str) else token
assert token.get("refresh_token") and token.get("token_type"), "not an rclone OAuth token"
lines = ["[prism-gdrive]", "type = drive", "scope = drive.file", "use_trash = false"]
lines += [f"{k} = {v}" for k, v in extra.items()]
lines.append("token = " + json.dumps(token, separators=(",", ":")))
sys.stdout.write("\n".join(lines) + "\n")
' > "$CONF.tmp"
chown "$U:$U" "$CONF.tmp"
chmod 0600 "$CONF.tmp"
mv "$CONF.tmp" "$CONF"
echo "installed $(stat -c '%U:%G %a' "$CONF") $CONF"
