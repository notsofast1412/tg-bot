#!/usr/bin/env bash
set -euo pipefail
umask 077
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$EUID" -ne 0 || "$project_root" != /opt/paintwatch ]]; then
  echo 'Запустите sudo bash /opt/paintwatch/scripts/install.sh из репозитория в /opt/paintwatch.' >&2
  exit 1
fi
if [[ ! -r /etc/os-release ]]; then
  echo 'Не удалось определить ОС. Установщик поддерживает Ubuntu 22.04 и 24.04.' >&2
  exit 1
fi
# shellcheck source=/dev/null
source /etc/os-release
if [[ "${ID:-}" != ubuntu || ! "${VERSION_ID:-}" =~ ^(22\.04|24\.04)$ ]]; then
  echo 'Установщик поддерживает Ubuntu 22.04 и 24.04.' >&2
  exit 1
fi
if systemctl is-active --quiet paintwatch; then
  echo 'Сначала остановите службу и сделайте backup: см. docs/operations.md.' >&2
  exit 1
fi
apt-get update
# Keep the parsed label stable on servers with a non-English locale.
if ! LC_ALL=C apt-cache policy python3.12-venv | awk '/Candidate:/ {if ($2 != "(none)") found=1} END {exit !found}'; then
  echo 'В подключённых репозиториях нет Python 3.12 с venv. Для Ubuntu 22.04 выполните подготовку из docs/ubuntu-22.04.md и повторите установку.' >&2
  exit 1
fi
DEBIAN_FRONTEND=noninteractive apt-get install -y python3.12 python3.12-venv ca-certificates tzdata
if ! id paintwatch >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/paintwatch --create-home --shell /usr/sbin/nologin paintwatch
fi
cd "$project_root"
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
install -d -m 700 -o paintwatch -g paintwatch data logs
chown -R paintwatch:paintwatch data logs
chmod 755 "$project_root"
chmod -R a+rX .venv paintwatch migrations scripts
chmod a+r alembic.ini
if [[ ! -e .env ]]; then
  install -m 600 -o paintwatch -g paintwatch .env.example .env
fi
chown paintwatch:paintwatch .env
chmod 600 .env
runuser -u paintwatch -- .venv/bin/python -m paintwatch.cli init-db
install -m 644 systemd/paintwatch.service /etc/systemd/system/paintwatch.service
systemctl daemon-reload
echo 'Установка завершена. Заполните /opt/paintwatch/.env (BOT_TOKEN, OWNER_ID). Затем sudo systemctl enable --now paintwatch.'
