#!/bin/bash
set -euo pipefail
[[ ${1:-} =~ ^[a-f0-9]{64}$ ]] || exit 2
ROOT=/data/validation/du-ranking-cadence/attempt-2
PYTHON=/opt/prism/current/.venv/bin/python
trap 'systemctl stop prism-universe.service; systemctl start prism-du-ranking-guard-stop.service' ERR
install -o root -g root -m 0644 "$ROOT/tools/prism-du-ranking-guard-stop.service" /etc/systemd/system/prism-du-ranking-guard-stop.service
sed "s/@BUDGET_SHA@/$1/g" "$ROOT/tools/prism-du-ranking-guard.service" > /run/prism-du-ranking-guard.service
install -o root -g root -m 0644 /run/prism-du-ranking-guard.service /etc/systemd/system/prism-du-ranking-guard.service
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/prism-du-ranking-guard.service /etc/systemd/system/prism-du-ranking-guard-stop.service
"$PYTHON" "$ROOT/tools/ranking_guard.py" --window "$ROOT" \
  --root "$ROOT/post-window-guard/preflight" --budget-sha "$1" --check
systemctl enable --now prism-du-ranking-guard.service
systemctl --no-pager show prism-du-ranking-guard -p ActiveState,MainPID,NRestarts
