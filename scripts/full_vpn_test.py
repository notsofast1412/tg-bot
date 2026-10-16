"""Temporary host IPv4 egress via the existing WireGuard namespace.

Install as a root-owned helper before running. Uses a five-minute systemd rollback.
Does not modify the host main routing table, SSH daemon, or host firewall.
IPv6 Internet access is blocked for the duration because this tunnel is IPv4-only.
"""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile


NS = 'paintbot-ru'
TABLE = '42420'
FIRST_PREF = 10000
V6_PREF = 10040
NFT_TABLE = 'paintbot_full_test'
UNIT = 'paintbot-full-vpn-rollback'
HELPER = Path('/usr/local/libexec/paintbot-full-vpn-test.py')
STATE = Path('/run/paintbot-full-vpn-test/state.json')
META = Path('/etc/paintbot-ru-proxy.json')


def run(*args, check=True, input=None):
    r = subprocess.run(args, input=input, capture_output=True, text=True, timeout=20)
    if check and r.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(args) + '\n' + r.stderr[:1000])
    return r


def inside(*args, **kwargs):
    return run('ip', 'netns', 'exec', NS, *args, **kwargs)


def save(state):
    STATE.parent.mkdir(mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=STATE.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream)
        os.replace(name, STATE)
    finally:
        Path(name).unlink(missing_ok=True)


def plan():
    connection = os.environ.get('SSH_CONNECTION', '').split()
    if len(connection) != 4:
        raise RuntimeError('Run in an active SSH session; SSH_CONNECTION is missing')
    client = ipaddress.IPv4Address(connection[0])
    server = ipaddress.IPv4Address(connection[2])
    if STATE.exists():
        raise RuntimeError('A test already exists; run rollback first')
    meta = json.loads(META.read_text())
    subnet = ipaddress.IPv4Network(meta['subnet'])
    host, proxy = ipaddress.IPv4Address(meta['host']), ipaddress.IPv4Address(meta['proxy'])
    if host not in subnet or proxy not in subnet or host == proxy:
        raise RuntimeError('Invalid VPN link metadata')
    routes = json.loads(run('ip', '-j', '-4', 'route', 'show', 'table', 'main').stdout)
    defaults = [r for r in routes if r.get('dst') == 'default' and r.get('gateway')]
    if len(defaults) != 1:
        raise RuntimeError('Expected exactly one main IPv4 default gateway')
    original = defaults[0]
    if original.get('dev') == 'pbru-host':
        raise RuntimeError('Main default route already uses the VPN link')
    server_route = json.loads(run('ip', '-j', '-4', 'route', 'get', str(client)).stdout)
    if not server_route or server_route[0].get('dev') != original['dev']:
        raise RuntimeError('SSH currently uses an unexpected route; test stopped')
    for family in ('-4', '-6'):
        rules = json.loads(run('ip', '-j', family, 'rule', 'show').stdout)
        if any(FIRST_PREF <= r.get('priority', -1) <= V6_PREF for r in rules):
            raise RuntimeError('Test rule priorities are occupied')
        table = run('ip', family, 'route', 'show', 'table', TABLE, check=False)
        if table.stdout.strip():
            raise RuntimeError('Test routing table is occupied')
        if table.returncode and 'FIB table does not exist' not in table.stderr:
            raise RuntimeError('Cannot inspect the test routing table')
    inside('ip', 'link', 'show', 'dev', 'pbru-wg')
    run('ip', 'link', 'show', 'dev', 'pbru-host')
    if inside('nft', 'list', 'table', 'ip', NFT_TABLE, check=False).returncode == 0:
        raise RuntimeError('Test NAT table already exists')
    for suffix in ('.timer', '.service'):
        state = run('systemctl', 'show', UNIT + suffix, '-p', 'LoadState', '--value').stdout.strip()
        if state != 'not-found':
            raise RuntimeError('Rollback unit name is occupied; inspect it before retrying')
    # Preserve directly connected networks and the two tunnel/SSH endpoints.
    exceptions = {str(client) + '/32', '95.161.4.67/32', str(subnet)}
    telegram = sorted({row[4][0] for row in socket.getaddrinfo(
        'api.telegram.org', 443, family=socket.AF_INET, type=socket.SOCK_STREAM)})
    if not telegram:
        raise RuntimeError('Cannot resolve Telegram IPv4 addresses; test stopped')
    exceptions.update(str(ipaddress.IPv4Address(address)) + '/32' for address in telegram)
    for route in routes:
        dst = route.get('dst', 'default')
        if dst != 'default' and route.get('scope') == 'link' and not route.get('gateway'):
            exceptions.add(str(ipaddress.IPv4Network(dst, strict=False)))
    if len(exceptions) >= V6_PREF - FIRST_PREF - 1:
        raise RuntimeError('Too many direct route exceptions')
    print('SSH client:', client, '; server:', server, flush=True)
    print('Main gateway preserved:', original['gateway'], original['dev'], flush=True)
    print('Direct exceptions:', ', '.join(sorted(exceptions)), flush=True)
    print('Telegram stays direct (current api.telegram.org IPv4):', ', '.join(telegram), flush=True)
    return {'exceptions': sorted(exceptions), 'subnet': str(subnet),
            'host': str(host), 'proxy': str(proxy), 'rules': [],
            'table4': False, 'table6': False, 'nft': False,
            'settings': {}, 'timer': False}


def rollback():
    if not STATE.exists():
        print('No active test state')
        return
    state = json.loads(STATE.read_text())
    failed = []
    for args in reversed(state['rules']):
        r = run(*args, check=False)
        if r.returncode and 'No such file' not in r.stderr:
            failed.append(' '.join(args))
    for family, flag in (('-4', 'table4'), ('-6', 'table6')):
        if state[flag]:
            if run('ip', family, 'route', 'flush', 'table', TABLE, check=False).returncode:
                failed.append(family + ' routing table')
    if state['nft']:
        r = inside('nft', 'delete', 'table', 'ip', NFT_TABLE, check=False)
        if r.returncode and 'No such file' not in r.stderr:
            failed.append('namespace NAT table')
    for key, value in state['settings'].items():
        if inside('sysctl', '-q', '-w', key + '=' + value, check=False).returncode:
            failed.append(key)
    if failed:
        raise RuntimeError('Rollback incomplete; state retained: ' + ', '.join(failed))
    if state['timer']:
        run('systemctl', 'stop', UNIT + '.timer', check=False)
    STATE.unlink()
    print('Test rolled back. Original host routing restored; bot VPN proxy remains configured.')


def start():
    for tool in ('ip', 'nft', 'sysctl', 'systemctl', 'systemd-run'):
        if not shutil.which(tool):
            raise RuntimeError('Missing program: ' + tool)
    state = plan()
    save(state)
    try:
        # Register rollback BEFORE changing any forwarding or route.
        run('systemd-run', '--unit=' + UNIT, '--on-active=300s',
            '--timer-property=AccuracySec=1s', '--collect',
            '/usr/bin/python3', '-I', str(HELPER), 'rollback')
        state['timer'] = True
        save(state)
        timer = run('systemctl', 'is-active', UNIT + '.timer').stdout.strip()
        if timer != 'active':
            raise RuntimeError('Rollback timer is not active')
        for key in ('net.ipv4.ip_forward', 'net.ipv4.conf.all.rp_filter',
                    'net.ipv4.conf.pbru-peer.rp_filter'):
            state['settings'][key] = inside('sysctl', '-n', key).stdout.strip()
        save(state)
        for key in state['settings']:
            inside('sysctl', '-q', '-w', key + ('=1' if key.endswith('ip_forward') else '=0'))
        state['nft'] = True
        save(state)
        inside('nft', '-f', '-', input='''table ip paintbot_full_test {
 chain postrouting {
  type nat hook postrouting priority srcnat; policy accept;
  iifname "pbru-peer" oifname "pbru-wg" masquerade
 }
}
''')
        state['table4'] = True
        save(state)
        run('ip', '-4', 'route', 'add', 'table', TABLE, state['subnet'],
            'dev', 'pbru-host', 'scope', 'link', 'src', state['host'])
        run('ip', '-4', 'route', 'add', 'table', TABLE, 'default', 'via', state['proxy'],
            'dev', 'pbru-host', 'src', state['host'])
        state['table6'] = True
        save(state)
        run('ip', '-6', 'route', 'add', 'table', TABLE, 'unreachable', 'default')
        # Keep SSH, WireGuard UDP, and connected networks on their original routes.
        for index, dest in enumerate(state['exceptions']):
            add_rule(state, '-4', FIRST_PREF + index, 'main', dest)
        add_rule(state, '-6', V6_PREF, TABLE)
        add_rule(state, '-4', V6_PREF - 1, TABLE)
        print('TEST ACTIVE: IPv4 Internet via WireGuard; IPv6 Internet blocked.', flush=True)
        print('Automatic rollback in 5 minutes. Manual rollback:', flush=True)
        print('python3 -I ' + str(HELPER) + ' rollback', flush=True)
    except BaseException:
        rollback()
        raise


def add_rule(state, family, pref, table, dest=None):
    selector = ['priority', str(pref)] + (['to', dest] if dest else []) + ['lookup', table]
    state['rules'].append(['ip', family, 'rule', 'del', *selector])
    save(state)
    run('ip', family, 'rule', 'add', *selector)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'rollback'])
    args = parser.parse_args()
    if os.geteuid() != 0 or Path(__file__).resolve() != HELPER:
        parser.error('Install as root-owned /usr/local/libexec/paintbot-full-vpn-test.py and run as root')
    os.umask(0o077)
    with open('/run/paintbot-full-vpn-test.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            start() if args.action == 'start' else rollback()
        except Exception as exc:
            print(type(exc).__name__ + ': ' + str(exc))
            raise SystemExit(1)
