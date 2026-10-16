#!/usr/bin/env bash
# Run only on the owner's actual Ubuntu server, not in the cloud agent.
set -euo pipefail
umask 077
if [[ "$EUID" -ne 0 || "$#" -ne 1 ]]; then
    echo 'Usage as root: bash scripts/setup_ru_vpn.sh /root/wg-russia.conf' >&2
    exit 1
fi
profile_path="$1"
cd "$(dirname "$0")/.."
if [[ "$(pwd -P)" != /opt/paintbot ]] || ! id paintbot >/dev/null 2>&1 || [[ ! -f .env || ! -x .venv/bin/python ]]; then
    echo 'Install Paint Monitor in /opt/paintbot first.' >&2
    exit 1
fi
runuser -u paintbot -- .venv/bin/python - <<'PY'
import inspect
from paintbot.config import Config
from paintbot.marketplaces.adapters import make_adapters
if not {'wb_proxy_url','ozon_proxy_url'}.issubset(Config.model_fields) or 'ozon_proxy_url' not in inspect.signature(make_adapters).parameters:
    raise SystemExit('Update Paint Monitor from the current source archive first; both proxy clients are required')
PY
source /etc/os-release
if [[ "$ID" != ubuntu ]] || [[ ! -d /run/systemd/system ]]; then
    echo 'Ubuntu with systemd is required.' >&2
    exit 1
fi
for unit in paintbot-ru-netns.service paintbot-ru-proxy.service; do
    if [[ -e "/etc/systemd/system/$unit" ]]; then
        echo 'VPN units already exist; use their start/status commands, do not overwrite them.' >&2
        exit 1
    fi
done
if [[ -e /etc/paintbot-ru/metadata.json ]]; then
    echo 'Prepared VPN configuration exists; inspect it before a repeated installation.' >&2
    exit 1
fi
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard-tools iproute2 python3 curl ca-certificates
install -d -m 755 /usr/local/libexec /run/netns
install -o root -g root -m 644 scripts/ru_vpn.py /usr/local/libexec/paintbot-ru-vpn.py
python3 -I /usr/local/libexec/paintbot-ru-vpn.py prepare --profile "$profile_path" --endpoint 95.161.4.67
install -m 644 systemd/paintbot-ru-netns.service /etc/systemd/system/paintbot-ru-netns.service
install -m 644 systemd/paintbot-ru-proxy.service /etc/systemd/system/paintbot-ru-proxy.service
systemctl daemon-reload
systemctl start paintbot-ru-netns.service paintbot-ru-proxy.service
python3 -I - <<'PY'
import json,socket,time
host=json.load(open('/etc/paintbot-ru-proxy.json'))['proxy']
for attempt in range(40):
    try:
        with socket.create_connection((host,3128),timeout=0.5):
            break
    except OSError:
        time.sleep(0.25)
else:
    raise SystemExit('Local VPN proxy did not become ready; bot settings were NOT changed')
PY
# A real TLS request through the restricted tunnel, never a direct fallback.
proxy_address="$(python3 -I -c 'import json; print("http://"+json.load(open("/etc/paintbot-ru-proxy.json"))["proxy"]+":3128")')"
if ! curl --proxy "$proxy_address" --noproxy '' --connect-timeout 15 --max-time 35 \
    --silent --show-error --output /dev/null 'https://www.ozon.ru/product/196525011/'; then
    echo 'Tunnel test failed. Bot proxy settings were NOT changed. Inspect the two VPN units and handshake.' >&2
    exit 1
fi
if ! ip netns exec paintbot-ru wg show pbru-wg latest-handshakes | \
    python3 -c 'import sys,time; rows=[line.split() for line in sys.stdin]; sys.exit(0 if any(len(r)==2 and int(r[1])>0 and 0<=int(time.time())-int(r[1])<180 for r in rows) else 1)'; then
    echo 'No recent WireGuard handshake. Bot settings were NOT changed.' >&2
    exit 1
fi
# Separate diagnostic inside the namespace; no provider keys or app tokens sent.
exit_address="$(ip netns exec paintbot-ru curl -4 --noproxy '*' --connect-timeout 10 --max-time 20 \
    --silent --show-error --max-filesize 128 'https://api.ipify.org' | \
    python3 -c 'import ipaddress,sys; print(ipaddress.IPv4Address(sys.stdin.read().strip()))')"
if [[ "$exit_address" != 95.161.4.67 ]]; then
    echo "Observed VPN exit is $exit_address, expected 95.161.4.67. Bot settings were NOT changed." >&2
    exit 1
fi
python3 -I /usr/local/libexec/paintbot-ru-vpn.py attach --env-file /opt/paintbot/.env
systemctl enable paintbot-ru-netns.service paintbot-ru-proxy.service
if systemctl is-active --quiet paintbot.service; then
    systemctl restart paintbot.service
fi
echo 'Tunnel responds; WB and Ozon configured. Verify actual prices: python -m scripts.verify_server'
