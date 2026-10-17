#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
mkdir -p var .cache .config .pki
if [[ ! -x .venv/bin/python ]]; then
    runtime_python="${PYTHON_BIN:-python3}"
    "$runtime_python" -c 'import sys; assert (3, 12) <= sys.version_info[:2] <= (3, 13), "Use Python 3.12 or 3.13 for the pinned dependencies"'
    "$runtime_python" -m venv .venv
fi
.venv/bin/python -c 'import sys; assert (3, 12) <= sys.version_info[:2] <= (3, 13), "Use Python 3.12 or 3.13 for the pinned dependencies"'
.venv/bin/python -m pip install --cache-dir /tmp/paintbot-pip-cache -r requirements.lock
# Separate Python environment for the browser worker; bot dependencies stay isolated.
if [[ ! -x .browser-probe/bin/python ]]; then
    .venv/bin/python -m venv .browser-probe
fi
.browser-probe/bin/python -m pip install --cache-dir /tmp/paintbot-pip-cache -r requirements-browser.lock
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m scripts.manage seed
.venv/bin/python -m pytest -q
if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
fi
echo 'Setup complete. Configure BOT_TOKEN and OWNER_ID securely in .env before starting.'
echo 'WB requires Google Chrome, Xvfb and xauth; install them as root with scripts/install_browser.sh.'
