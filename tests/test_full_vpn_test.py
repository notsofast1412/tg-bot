import json
import subprocess

import pytest

from scripts import full_vpn_test as vpn


def test_missing_ssh_stops_before_network_changes(monkeypatch, tmp_path):
    monkeypatch.delenv('SSH_CONNECTION', raising=False)
    monkeypatch.setattr(vpn, 'STATE', tmp_path / 'state.json')
    monkeypatch.setattr(vpn, 'run', lambda *a, **kw: pytest.fail('No network commands expected'))
    with pytest.raises(RuntimeError, match='SSH_CONNECTION'):
        vpn.plan()


def test_plan_preserves_ssh_tunnel_telegram_and_connected_networks(monkeypatch, tmp_path):
    monkeypatch.setenv('SSH_CONNECTION', '198.51.100.9 40000 201.51.22.210 22')
    monkeypatch.setattr(vpn, 'STATE', tmp_path / 'state.json')
    meta = tmp_path / 'meta.json'
    meta.write_text(json.dumps({'subnet': '10.203.254.0/30', 'host': '10.203.254.1', 'proxy': '10.203.254.2'}))
    monkeypatch.setattr(vpn, 'META', meta)
    monkeypatch.setattr(vpn.socket, 'getaddrinfo', lambda *a, **kw: [(None, None, None, None, ('149.154.167.220', 443))])
    def run(*args, **kwargs):
        output = ''
        if args[:5] == ('ip', '-j', '-4', 'route', 'show'):
            output = json.dumps([
                {'dst': 'default', 'gateway': '201.51.22.1', 'dev': 'eth0'},
                {'dst': '201.51.22.0/24', 'scope': 'link', 'dev': 'eth0'},
                {'dst': '85.193.93.193', 'gateway': '201.51.22.1', 'dev': 'eth0'},
            ])
        elif args[:5] == ('ip', '-j', '-4', 'route', 'get'):
            output = '[{"dev":"eth0"}]'
        elif args[0] == 'systemctl':
            output = 'not-found\n'
        elif 'rule' in args:
            output = '[]'
        return subprocess.CompletedProcess(args, 0, output, '')
    monkeypatch.setattr(vpn, 'run', run)
    monkeypatch.setattr(vpn, 'inside', lambda *a, **kw: subprocess.CompletedProcess(a, 1 if a[0] == 'nft' else 0, '', ''))
    state = vpn.plan()
    assert {'198.51.100.9/32', '95.161.4.67/32', '149.154.167.220/32', '201.51.22.0/24'} <= set(state['exceptions'])
    # Host DNS routes via the old gateway must NOT escape the full VPN policy.
    assert '85.193.93.193/32' not in state['exceptions']


def test_start_registers_timer_before_network_changes_and_keeps_main_table(monkeypatch):
    state = {'exceptions': ['198.51.100.9/32', '149.154.167.220/32'],
             'subnet': '10.203.254.0/30', 'host': '10.203.254.1', 'proxy': '10.203.254.2',
             'rules': [], 'table4': False, 'table6': False, 'nft': False, 'settings': {}, 'timer': False}
    monkeypatch.setattr(vpn, 'plan', lambda: state)
    monkeypatch.setattr(vpn, 'save', lambda s: None)
    monkeypatch.setattr(vpn.shutil, 'which', lambda tool: '/usr/bin/' + tool)
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        output = 'active\n' if args[:2] == ('systemctl', 'is-active') else ''
        if 'sysctl' in args and '-n' in args:
            output = '0\n'
        return subprocess.CompletedProcess(args, 0, output, '')
    monkeypatch.setattr(vpn, 'run', run)
    vpn.start()
    assert calls[0][0][0] == 'systemd-run'
    assert '--on-active=300s' in calls[0][0]
    route_changes = [args for args, _ in calls if args[0] == 'ip' and 'route' in args and 'add' in args]
    assert route_changes and all('42420' in args for args in route_changes)
    assert any('-6' in args and 'unreachable' in args for args in route_changes)
    # Namespace forwarding/NAT only; no host firewall or host sysctl writes.
    assert all(args[0] not in ('sysctl', 'nft') for args, _ in calls)
    assert 'iifname "pbru-peer" oifname "pbru-wg" masquerade' in next(
        kw['input'] for args, kw in calls if 'nft' in args)
    assert calls[-1][0][:4] == ('ip', '-4', 'rule', 'add')
    assert '10039' in calls[-1][0]


def test_rollback_restores_only_owned_rules_and_namespace_settings(monkeypatch, tmp_path):
    statefile = tmp_path / 'state.json'
    delete = ['ip', '-4', 'rule', 'del', 'priority', '10039', 'lookup', '42420']
    statefile.write_text(json.dumps({'rules': [delete], 'table4': True, 'table6': True,
        'nft': True, 'settings': {'net.ipv4.ip_forward': '0'}, 'timer': True}))
    monkeypatch.setattr(vpn, 'STATE', statefile)
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(vpn, 'run', run)
    vpn.rollback()
    assert tuple(delete) in calls
    assert ('ip', '-4', 'route', 'flush', 'table', '42420') in calls
    assert ('ip', 'netns', 'exec', 'paintbot-ru', 'sysctl', '-q', '-w', 'net.ipv4.ip_forward=0') in calls
    assert not any('main' in args for args in calls)
    assert not statefile.exists()
