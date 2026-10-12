#!/usr/bin/env bash
# One-time (idempotent) host preparation for a Prism VPS. Run as root on a fresh Debian 12 or
# Ubuntu 22.04/24.04 server: provider-agnostic (OVH, Hetzner, DigitalOcean, ...).
#   bootstrap.sh              packages, prism user, /data layout, pinned uv/Python/supercronic,
#                             key-only SSH, nftables inbound deny, journald caps, auto-updates,
#                             time sync, systemd units (installed, NOT started)
#   bootstrap.sh --lock-root  afterwards, once a non-root sudo SSH login is verified: deny root SSH
# It never creates a database, claims authority, opens a port beyond SSH, or starts Prism.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
UV_VERSION=${UV_VERSION:-0.12.0}
PYTHON_VERSION=${PYTHON_VERSION:-3.12.3}
SUPERCRONIC_VERSION=v0.2.49   # same pin as deploy/railway/Dockerfile
SUPERCRONIC_SHA256=a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

if [ "${1:-}" = --lock-root ]; then
  sed -i 's/^PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config.d/10-prism.conf
  sshd -t && systemctl reload ssh
  echo "root SSH login disabled"
  exit 0
fi

. /etc/os-release
case "$ID:$VERSION_ID" in
  debian:12|debian:13|ubuntu:22.04|ubuntu:24.04) ;;
  *) echo "unsupported OS $ID $VERSION_ID (Debian stable or Ubuntu LTS expected)" >&2; exit 1 ;;
esac
[ "$(uname -m)" = x86_64 ] || { echo "x86_64 expected (pinned binaries)" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl tar gzip rsync jq sudo \
  nftables chrony unattended-upgrades fio procps sysstat

# Dedicated unprivileged service user; no password, no login shell.
id prism >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/prism \
  --shell /usr/sbin/nologin prism
install -d -o root -g root -m 0755 /opt/prism /opt/prism/releases
install -d -o prism -g prism -m 0755 /opt/prism/python
install -d -o root -g prism -m 0750 /etc/prism
for d in /data /data/state /data/logs /data/backups /data/raw /data/microstructure \
         /data/free_event_sources /data/context_gateway /data/validation; do
  install -d -o prism -g prism -m 0750 "$d"
done
[ -f /etc/prism/prism.env ] || install -o root -g prism -m 0640 "$HERE/etc/prism.env.example" /etc/prism/prism.env
[ -f /etc/prism/secrets.env ] || install -o root -g prism -m 0640 /dev/null /etc/prism/secrets.env

# Pinned uv: checksum published with the same GitHub release (set UV_SHA256 to pin it here).
if ! /usr/local/bin/uv --version 2>/dev/null | grep -q "uv $UV_VERSION"; then
  tmp=$(mktemp -d)
  base="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz"
  curl -fsSL -o "$tmp/uv.tgz" "$base"
  want=${UV_SHA256:-$(curl -fsSL "$base.sha256" | awk '{print $1}')}
  echo "$want  $tmp/uv.tgz" | sha256sum -c -
  tar -xzf "$tmp/uv.tgz" -C "$tmp"
  install -m 0755 "$tmp"/uv-x86_64-unknown-linux-gnu/uv "$tmp"/uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/
  rm -rf "$tmp"
fi
# Pinned CPython in /opt (not /home: units run with ProtectHome=yes).
sudo -u prism env UV_PYTHON_INSTALL_DIR=/opt/prism/python /usr/local/bin/uv python install "$PYTHON_VERSION"

if ! echo "$SUPERCRONIC_SHA256  /usr/local/bin/supercronic" | sha256sum -c - >/dev/null 2>&1; then
  curl -fsSL -o /usr/local/bin/supercronic \
    "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64"
  echo "$SUPERCRONIC_SHA256  /usr/local/bin/supercronic" | sha256sum -c -
  chmod 0755 /usr/local/bin/supercronic
fi

# Security baseline. sshd config is validated before reload so a typo cannot lock you out.
install -m 0644 "$HERE/etc/sshd-prism.conf" /etc/ssh/sshd_config.d/10-prism.conf
sshd -t && systemctl reload ssh
install -m 0755 "$HERE/etc/nftables.conf" /etc/nftables.conf
nft -c -f /etc/nftables.conf && systemctl enable --now nftables && nft -f /etc/nftables.conf
install -d /etc/systemd/journald.conf.d
install -m 0644 "$HERE/etc/journald-prism.conf" /etc/systemd/journald.conf.d/prism.conf
systemctl restart systemd-journald
install -m 0644 "$HERE/etc/20auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades
systemctl enable --now unattended-upgrades chrony
timedatectl set-timezone UTC

# Units are installed but not enabled/started: the first release + profile check come first.
install -m 0644 "$HERE"/systemd/*.service "$HERE"/systemd/prism.target /etc/systemd/system/
systemctl daemon-reload
echo "bootstrap complete. Next: deploy a release (deploy/vps/deploy.sh), review /etc/prism/prism.env"
echo "(PRISM_AUTHORITY=none), then: systemctl enable --now prism.target"
