#!/usr/bin/env bash
# First deployment into a NEW /opt/paintbot directory. Does not start the bot.
set -euo pipefail
umask 077
if [[ "$EUID" -ne 0 ]]; then
    echo 'Run as root on the target Ubuntu server.' >&2
    exit 1
fi
cd "$(dirname "$0")/.."
if [[ "$(pwd -P)" != /opt/paintbot ]]; then
    echo 'Extract the source into /opt/paintbot first; no other path is modified.' >&2
    exit 1
fi
if [[ ! -f /etc/os-release ]]; then
    echo 'Ubuntu is required.' >&2
    exit 1
fi
source /etc/os-release
if [[ "$ID" != ubuntu ]]; then
    echo 'This bootstrap targets Ubuntu only.' >&2
    exit 1
fi
if id paintbot >/dev/null 2>&1; then
    echo 'Account paintbot already exists. Use the documented update procedure; existing deployment is preserved.' >&2
    exit 1
fi
for unit in paintbot.service paintbot-backup.service paintbot-backup.timer; do
    if [[ -e "/etc/systemd/system/$unit" ]]; then
        echo 'A Paint Monitor unit already exists; use the update procedure.' >&2
        exit 1
    fi
done
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv ca-certificates
useradd --system --user-group --home-dir /opt/paintbot --shell /usr/sbin/nologin paintbot
chown -R paintbot:paintbot /opt/paintbot
chmod 750 /opt/paintbot
# Isolated toolchain; never replace /usr/bin/python3 or configure a VPN/firewall.
runuser -u paintbot -- python3 -m venv .bootstrap
runuser -u paintbot -- .bootstrap/bin/python -m pip install --cache-dir /opt/paintbot/.bootstrap-cache 'uv==0.12.23'
runuser -u paintbot -- env UV_PYTHON_INSTALL_DIR=/opt/paintbot/.python UV_CACHE_DIR=/opt/paintbot/.uv-cache \
    .bootstrap/bin/uv python install --no-bin 3.12.14
runuser -u paintbot -- env UV_PYTHON_INSTALL_DIR=/opt/paintbot/.python UV_CACHE_DIR=/opt/paintbot/.uv-cache \
    .bootstrap/bin/uv venv --python 3.12.14 --seed .venv
runuser -u paintbot -- bash scripts/install.sh
install -m 644 systemd/paintbot.service /etc/systemd/system/paintbot.service
install -m 644 systemd/paintbot-backup.service /etc/systemd/system/paintbot-backup.service
install -m 644 systemd/paintbot-backup.timer /etc/systemd/system/paintbot-backup.timer
systemctl daemon-reload
echo 'Installed without starting. Next: runuser -u paintbot -- .venv/bin/python -m scripts.configure'
