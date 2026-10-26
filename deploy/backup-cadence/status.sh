#!/bin/sh
# JSON report per authority. Run with sudo on OVH; report failures independently.
set -eu
report_status=0
for authority in lab research; do
    case "$authority" in
        lab) release=/opt/prism/current ;;
        research) release=/opt/prism/research/current ;;
    esac
    if ! env PYTHONPATH="$release/src" "$release/.venv/bin/python" /opt/prism/backup-cadence/cadence.py status "$authority"; then
        printf '{"authority":"%s","status":"report_failed"}\n' "$authority"
        report_status=1
    fi
done
exit "$report_status"
