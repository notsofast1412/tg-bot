#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
mkdir -p var/backups
.venv/bin/python -m scripts.manage backup "var/backups/paintbot-$(date -u +%Y%m%dT%H%M%S)-$$.db"
# Keep the latest 30 completed backups. No arbitrary user paths are removed.
.venv/bin/python - <<'PY'
from pathlib import Path
files = sorted(Path('var/backups').glob('paintbot-*.db'), key=lambda p: p.stat().st_mtime, reverse=True)
for path in files[30:]:
    path.unlink()
PY
