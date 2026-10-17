#!/usr/bin/env bash
# Run the downloaded helper as root; pin the archive to the same reviewed commit.
set -Eeuo pipefail
umask 077
if [[ $EUID -ne 0 || ! ${1:-} =~ ^[a-f0-9]{40}$ ]]; then
    echo 'Usage as root: bash update_wb_browser.sh COMMIT_SHA' >&2
    exit 1
fi
release=$1
cd /opt/paintbot
test -f .env
update_dir=$(mktemp -d /root/paintbot-wb-update-XXXXXX)
trap 'echo "Update interrupted. Backup and source: $update_dir" >&2' ERR
curl --fail --location --retry 3 --connect-timeout 15 --max-time 180 \
    "https://codeload.github.com/notsofast1412/tg-bot/tar.gz/$release" -o "$update_dir/source.tar.gz"
mkdir "$update_dir/source" "$update_dir/old-source" "$update_dir/old-units"
tar -xzf "$update_dir/source.tar.gz" -C "$update_dir/source" --strip-components=1
test -f "$update_dir/source/browser-extension/manifest.json"
test -f "$update_dir/source/scripts/setup_wb_browser.sh"
for executable in /usr/bin/google-chrome-stable xvfb-run xauth x11vnc; do
    command -v "$executable" >/dev/null || { echo "Missing: $executable" >&2; exit 1; }
done
test -s var/vnc.pass
if ! command -v xdotool >/dev/null; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y xdotool
fi
systemctl stop paintbot.service paintbot-backup.timer paintbot-backup.service
if systemctl cat paintbot-browser.service >/dev/null 2>&1; then
    systemctl stop paintbot-browser.service
fi
tar -C /opt/paintbot --exclude='./.venv' --exclude='./.browser-probe' \
    --exclude='./.python' --exclude='./.bootstrap' --exclude='./.bootstrap-cache' \
    --exclude='./.uv-cache' --exclude='./.cache' --exclude='./var/chromium' \
    --exclude='./var/browser-cache' -czf "$update_dir/before-update.tar.gz" .
for unit in paintbot.service paintbot-backup.service paintbot-backup.timer paintbot-browser.service; do
    if [[ -f /etc/systemd/system/$unit ]]; then
        cp -a "/etc/systemd/system/$unit" "$update_dir/old-units/"
    fi
done
echo "Backup: $update_dir/before-update.tar.gz"
for item in paintbot scripts tests docs migrations systemd browser-extension; do
    if [[ -e $item ]]; then mv "$item" "$update_dir/old-source/"; fi
done
cp -a "$update_dir/source/." /opt/paintbot/
chown -R paintbot:paintbot /opt/paintbot
runuser -u paintbot -- bash scripts/install.sh
bash scripts/setup_wb_browser.sh
install -m 644 systemd/paintbot.service systemd/paintbot-backup.service \
    systemd/paintbot-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable paintbot.service paintbot-backup.timer
systemctl restart paintbot.service
systemctl start paintbot-backup.timer
sleep 3
systemctl is-active --quiet paintbot.service
systemctl is-active --quiet paintbot-browser.service
echo "Code installed: $release"
echo 'Finish Chrome extension setup in VNC: see docs/WB_BROWSER.md.'
echo "Backup: $update_dir/before-update.tar.gz"
