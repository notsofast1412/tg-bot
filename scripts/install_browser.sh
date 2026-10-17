#!/usr/bin/env bash
# Optional diagnostic browser; does not change bot configuration or start services.
set -euo pipefail
if [[ "$EUID" -ne 0 ]]; then
    echo 'Run this installer as root on the Ubuntu VM.' >&2
    exit 1
fi
source /etc/os-release
if [[ "$ID" != ubuntu || "$(dpkg --print-architecture)" != amd64 ]]; then
    echo 'This installer requires Ubuntu amd64.' >&2
    exit 1
fi
apt-get update
apt-get install -y ca-certificates curl gnupg
install -d -m 755 /etc/apt/keyrings
browser_key=$(mktemp)
browser_keyring=$(mktemp)
trap 'rm -f "$browser_key" "$browser_keyring"' EXIT
curl --fail --location --silent --show-error --max-time 60 \
    https://dl.google.com/linux/linux_signing_key.pub -o "$browser_key"
gpg --batch --yes --dearmor --output "$browser_keyring" "$browser_key"
install -m 644 "$browser_keyring" /etc/apt/keyrings/paintbot-google-chrome.gpg
repo_file=/etc/apt/sources.list.d/google-chrome.list
repo_line='deb [arch=amd64 signed-by=/etc/apt/keyrings/paintbot-google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main'
# Reuse an existing Google repository instead of creating a conflicting duplicate.
if ! grep -REqs '^[[:space:]]*(deb .*|URIs:[[:space:]]*)https?://dl\.google\.com/linux/chrome/deb/?' \
    /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
    if [[ -e "$repo_file" ]]; then
        echo 'The proposed repository file already exists with different contents; inspect it first.' >&2
        exit 1
    fi
    printf '%s\n' "$repo_line" > "$repo_file"
fi
apt-get update
apt-get install -y google-chrome-stable
google-chrome-stable --version
echo 'Browser installed. The bot and existing browser profiles were not changed.'
