#!/bin/sh
# Run as root on OVH with this directory as the argument. No application release,
# policy, runtime environment, credential or trading process is changed.
set -eu
SOURCE=${1:?usage: install.sh SOURCE_DIRECTORY}
test "$(id -u)" = 0
install -d -m 0755 /opt/prism/backup-cadence
install -m 0644 "$SOURCE/cadence.py" /opt/prism/backup-cadence/cadence.py
for unit in prism-lab-backup-export.service prism-lab-backup-export.timer prism-research-backup-reconcile.service; do
    install -m 0644 "$SOURCE/$unit" "/etc/systemd/system/$unit"
done
for item in 'prism-research-backup.service research-cadence.conf' 'prism-research-backup-source.service research-source.conf' 'prism-research-backup-ack.service research-ack.conf'; do
    set -- $item
    install -d -m 0755 "/etc/systemd/system/$1.d"
    install -m 0644 "$SOURCE/$2" "/etc/systemd/system/$1.d/zz-cadence.conf"
done
install -d -m 0755 /var/lib/prism-research-backup/cadence/research
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/prism-lab-backup-export.service /etc/systemd/system/prism-lab-backup-export.timer /etc/systemd/system/prism-research-backup-reconcile.service
systemctl enable --now prism-lab-backup-export.timer
