#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
if [[ $EUID -ne 0 || $PWD != /opt/paintbot ]]; then
    echo 'Run as root from /opt/paintbot on the Ubuntu server.' >&2
    exit 1
fi
for executable in /usr/bin/google-chrome-stable xvfb-run xauth x11vnc; do
    command -v "$executable" >/dev/null || { echo "Missing: $executable" >&2; exit 1; }
done
test -s var/vnc.pass
mkdir -p var/wb-extension .cache .config .pki
.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
import re
import secrets
import shutil

key_path = Path('var/wb-browser.key')
if not key_path.exists():
    with key_path.open('x') as file:
        file.write(secrets.token_hex(32))
key = key_path.read_text().strip()
if not re.fullmatch(r'[a-f0-9]{64}', key):
    raise SystemExit('Invalid existing bridge key; inspect locally without sharing its value.')
os.chmod(key_path, 0o600)
for source in Path('browser-extension').iterdir():
    if source.is_file():
        shutil.copyfile(source, Path('var/wb-extension') / source.name)
Path('var/wb-extension/config.js').write_text('const PAINTBOT_KEY = ' + json.dumps(key) + ';\n')
for path in Path('var/wb-extension').iterdir():
    os.chmod(path, 0o600)
os.chmod('var/wb-extension', 0o700)
PY
chown -R paintbot:paintbot var/wb-extension .cache .config .pki
chown paintbot:paintbot var/wb-browser.key
install -m 644 systemd/paintbot-browser.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now paintbot-browser.service
echo 'In VNC: load unpacked extension /opt/paintbot/var/wb-extension in chrome://extensions.'
echo 'Then open the WB tab and close/reopen DevTools with F12. See docs/WB_BROWSER.md.'
