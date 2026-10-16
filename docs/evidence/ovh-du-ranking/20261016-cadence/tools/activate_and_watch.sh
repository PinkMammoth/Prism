#!/bin/bash
set -euo pipefail
/opt/prism/current/.venv/bin/python /data/validation/du-ranking-cadence/attempt-2/tools/activate_reviewed.py 69d3bc695944c4a88bf5f023e6d251bbf1a7c4120252f1ea0d5199085f91ce44
if ! systemd-run --unit=prism-du-ranking-watch-attempt2 \
  --property=OnFailure=prism-du-ranking-emergency-stop-attempt2.service \
  --property=MemoryMax=512M --property=CPUQuota=25% --property=Nice=19 \
  --property=NoNewPrivileges=yes --property=ProtectHome=yes \
  --property=ProtectSystem=strict --property='ReadWritePaths=/data /etc/prism' \
  --property='InaccessiblePaths=/var/lib/prism-research /etc/prism-research-backup' \
  --property=PrivateTmp=yes --property=Restart=no --property=OOMPolicy=stop \
  /opt/prism/current/.venv/bin/python /data/validation/du-ranking-cadence/attempt-2/tools/ranking_watch.py \
  --root /data/validation/du-ranking-cadence/attempt-2; then
  systemctl start prism-du-ranking-emergency-stop-attempt2.service
  exit 1
fi
systemctl --no-pager show prism-universe prism-du-ranking-watch-attempt2 -p ActiveState,MainPID,NRestarts
