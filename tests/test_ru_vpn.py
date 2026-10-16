import asyncio
import base64
import json
import socket
import subprocess

import pytest

from scripts import ru_vpn


FAKE_PRIVATE = base64.b64encode(b"A" * 32).decode()
FAKE_PUBLIC = base64.b64encode(b"B" * 32).decode()
PROFILE = f"""[Interface]
PrivateKey = {FAKE_PRIVATE}
Address = 10.17.0.2/32, fd00::2/128
DNS = 10.17.0.1
MTU = 1420

[Peer]
PublicKey = {FAKE_PUBLIC}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = 95.161.4.67:51820
"""


def test_profile_becomes_setconf_without_routes_dns_hooks_or_ipv6_default():
    result = ru_vpn.parse_profile(PROFILE, "95.161.4.67")
    assert result["addresses"] == ["10.17.0.2/32"]
    assert result["dns"] == ["10.17.0.1"]
    assert result["port"] == 51820
    assert "PrivateKey = " + FAKE_PRIVATE in result["wg_config"]
    for unsupported in ("DNS", "Address", "MTU", "::/0", "Table", "PostUp"):
        assert unsupported not in result["wg_config"]


@pytest.mark.parametrize("old,new", [
    ("[Peer]", "PostUp = echo NEVER_EXECUTE\n\n[Peer]"),
    ("MTU = 1420", "Jc = 4\nMTU = 1420"),
    ("95.161.4.67:51820", "95.161.4.68:51820"),
    ("95.161.4.67:51820", "95.161.4.67:0"),
    ("0.0.0.0/0, ::/0", "10.0.0.0/8"),
    ("DNS = 10.17.0.1", "DNS = 127.0.0.53"),
    (FAKE_PRIVATE, "NOT_A_VALID_PRIVATE_KEY"),
    ("MTU = 1420", "MTU = 9000"),
])
def test_unsupported_profile_rejected_without_exposing_keys(old, new):
    with pytest.raises(ru_vpn.SetupError) as error:
        ru_vpn.parse_profile(PROFILE.replace(old, new), "95.161.4.67")
    assert FAKE_PRIVATE not in str(error.value)
    assert "NOT_A_VALID_PRIVATE_KEY" not in str(error.value)


def test_subnet_selection_avoids_host_and_tunnel_routes():
    result = ru_vpn.choose_subnet([{"dst": "default"}, {"dst": "10.0.0.0/8"}], ["172.31.250.8/24"])
    assert result["subnet"] == "192.168.253.0/30"
    with pytest.raises(ru_vpn.SetupError):
        ru_vpn.choose_subnet([{"dst": "10.0.0.0/8"}, {"dst": "172.16.0.0/12"}, {"dst": "192.168.0.0/16"}], [])


def test_runtime_adds_default_only_inside_namespace_and_creates_udp_interface_in_host(tmp_path, monkeypatch):
    private, marker = tmp_path / "private", tmp_path / "run" / "owned"
    private.mkdir()
    metadata = {"addresses": ["10.17.0.2/32"], "mtu": 1420,
                "link": {"subnet": "10.203.254.0/30", "host": "10.203.254.1", "proxy": "10.203.254.2"}}
    (private / "metadata.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(ru_vpn, "PRIVATE", private)
    monkeypatch.setattr(ru_vpn, "MARKER", marker)
    calls = []
    def run(*args, check=True, capture=False):
        calls.append(args)
        if args[:3] == ("ip", "link", "show"):
            return subprocess.CompletedProcess(args, 1, "", "")
        output = "[]" if args[:3] == ("ip", "-j", "-4") else ""
        return subprocess.CompletedProcess(args, 0, output, "")
    monkeypatch.setattr(ru_vpn, "command", run)
    ru_vpn.start()
    defaults = [c for c in calls if "default" in c]
    assert defaults == [("ip", "netns", "exec", ru_vpn.NS, "ip", "route", "add", "default", "dev", ru_vpn.WG_LINK)]
    assert ("ip", "link", "add", ru_vpn.WG_LINK, "type", "wireguard") in calls
    assert ("ip", "link", "set", ru_vpn.WG_LINK, "netns", ru_vpn.NS) in calls
    assert not any(c[0] in ("iptables", "nft", "sysctl") for c in calls)
    assert marker.exists()


def test_proxy_env_update_preserves_token_and_other_settings():
    original = "BOT_TOKEN='DO_NOT_PRINT'\nOWNER_ID=1\nWB_PROXY_URL='old'\nexport OZON_PROXY_URL=old\nDATABASE_PATH=var/x.db\n"
    updated = ru_vpn.update_proxy_env(original, "http://10.203.254.2:3128")
    assert "BOT_TOKEN='DO_NOT_PRINT'" in updated
    assert "OWNER_ID=1" in updated
    assert "DATABASE_PATH=var/x.db" in updated
    assert updated.count("WB_PROXY_URL=") == 1
    assert updated.count("OZON_PROXY_URL=") == 1
    assert "old" not in updated


def test_stop_does_not_delete_unowned_namespaces(tmp_path, monkeypatch):
    monkeypatch.setattr(ru_vpn, "MARKER", tmp_path / "not-owned")
    def unexpected(*args, **kwargs):
        pytest.fail("An unowned namespace or interface must not be deleted")
    monkeypatch.setattr(ru_vpn, "command", unexpected)
    ru_vpn.stop()


def test_start_refuses_existing_interface_before_any_network_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(ru_vpn, "MARKER", tmp_path / "not-owned")
    calls = []
    def busy(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(ru_vpn, "command", busy)
    with pytest.raises(ru_vpn.SetupError, match="занято"):
        ru_vpn.start()
    assert calls == [("ip", "link", "show", "dev", ru_vpn.HOST_LINK)]


@pytest.mark.parametrize("line", [
    b"GET http://ozon.ru/ HTTP/1.1", b"CONNECT localhost:443 HTTP/1.1",
    b"CONNECT 127.0.0.1:443 HTTP/1.1", b"CONNECT api.telegram.org:443 HTTP/1.1",
    b"CONNECT www.ozon.ru:22 HTTP/1.1", b"CONNECT www.ozon.ru.evil.com:443 HTTP/1.1",
    b"CONNECT user:pass@ozon.ru:443 HTTP/1.1",
])
def test_proxy_rejects_non_marketplace_or_non_https_connect(line):
    assert ru_vpn.connect_target(line + b"\r\n\r\n", "10.0.0.1", "10.0.0.1") is None


def test_proxy_allows_only_host_peer_and_exact_https_hosts():
    header = b"CONNECT www.ozon.ru:443 HTTP/1.1\r\nHost: www.ozon.ru\r\n\r\n"
    assert ru_vpn.connect_target(header, "10.0.0.1", "10.0.0.1") == "www.ozon.ru"
    assert ru_vpn.connect_target(header, "10.0.0.2", "10.0.0.1") is None
    assert ru_vpn.connect_target(header + b"X" * 16384, "10.0.0.1", "10.0.0.1") is None


async def test_connect_proxy_relays_opaque_bytes_without_tls_interception():
    async def echo(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.close()
    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]
    destinations = []
    async def connector(host, port, family):
        destinations.append((host, port, family))
        return await asyncio.open_connection("127.0.0.1", up_port)
    proxy = await asyncio.start_server(ru_vpn.proxy_handler("127.0.0.1", connector), "127.0.0.1", 0, limit=16384)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.sockets[0].getsockname()[1])
        writer.write(b"CONNECT www.ozon.ru:443 HTTP/1.1\r\nHost: www.ozon.ru\r\n\r\n")
        await writer.drain()
        assert b"200 Connection Established" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        opaque = b"\x16\x03\x01opaque-ciphertext\x00\xff"
        writer.write(opaque)
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(len(opaque)), 2) == opaque
        assert destinations == [("www.ozon.ru", 443, socket.AF_INET)]
        writer.close()
        await writer.wait_closed()
    finally:
        proxy.close()
        upstream.close()
        await proxy.wait_closed()
        await upstream.wait_closed()


async def test_connect_proxy_failure_does_not_return_success():
    async def failed(*args, **kwargs):
        raise OSError("unreachable tunnel")
    proxy = await asyncio.start_server(ru_vpn.proxy_handler("127.0.0.1", failed), "127.0.0.1", 0)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.sockets[0].getsockname()[1])
        writer.write(b"CONNECT card.wb.ru:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert b"502 Bad Gateway" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        writer.close()
        await writer.wait_closed()
    finally:
        proxy.close()
        await proxy.wait_closed()
