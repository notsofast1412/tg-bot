"""Restricted WireGuard namespace and CONNECT proxy, stdlib only.

Runs on the owner's Ubuntu server, never in the cloud agent container.
Runtime copy belongs to root outside the application's writable checkout.
"""
import argparse
import asyncio
import base64
import configparser
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile

NS = "paintbot-ru"
HOST_LINK, NS_LINK, WG_LINK = "pbru-host", "pbru-peer", "pbru-wg"
PRIVATE = Path("/etc/paintbot-ru")
PUBLIC = Path("/etc/netns/paintbot-ru")
PROXY_META = Path("/etc/paintbot-ru-proxy.json")
MARKER = Path("/run/paintbot-ru/owned")
DOMAINS = frozenset(("card.wb.ru", "www.wildberries.ru", "ozon.ru", "www.ozon.ru"))


class SetupError(ValueError):
    pass


def key_valid(value):
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (ValueError, TypeError):
        return False


def parse_profile(text, expected_endpoint):
    """Never execute wg-quick hooks, print keys, or apply host default routes."""
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(text)
        if parser.defaults() or set(parser.sections()) != {"Interface", "Peer"}:
            raise SetupError("Нужен стандартный WireGuard-профиль с одним Interface и одним Peer")
        interface, peer = dict(parser["Interface"]), dict(parser["Peer"])
        if set(interface) - {"PrivateKey", "Address", "DNS", "MTU", "ListenPort"}:
            raise SetupError("Профиль содержит нестандартные поля или hooks; они не будут выполняться")
        if set(peer) - {"PublicKey", "PresharedKey", "AllowedIPs", "Endpoint", "PersistentKeepalive"}:
            raise SetupError("Нестандартные поля Peer не поддерживаются")
        if not key_valid(interface.get("PrivateKey")) or not key_valid(peer.get("PublicKey")):
            raise SetupError("Некорректный формат ключей WireGuard")
        if "PresharedKey" in peer and not key_valid(peer["PresharedKey"]):
            raise SetupError("Некорректный формат PresharedKey")
        endpoint = peer.get("Endpoint", "")
        match = re.fullmatch(r"([^\s:]+):(\d{1,5})", endpoint)
        if not match or not 1 <= int(match[2]) <= 65535:
            raise SetupError("Нужен IPv4 Endpoint с корректным портом")
        expected = ipaddress.IPv4Address(expected_endpoint)
        if not expected.is_global:
            raise SetupError("Endpoint должен быть публичным IPv4-адресом")
        # A DNS endpoint must resolve to the explicitly supplied Russian address.
        try:
            actual = {ipaddress.IPv4Address(match[1])}
        except ipaddress.AddressValueError:
            actual = {ipaddress.IPv4Address(row[4][0]) for row in socket.getaddrinfo(match[1], None, socket.AF_INET)}
        if expected not in actual:
            raise SetupError("Endpoint профиля не совпадает с указанным российским узлом")
        addresses = [ipaddress.ip_interface(v.strip()) for v in interface.get("Address", "").split(",") if v.strip()]
        addresses = [v for v in addresses if v.version == 4]
        if not addresses or any(v.ip.is_unspecified or v.ip.is_multicast or v.ip.is_loopback for v in addresses):
            raise SetupError("В профиле необходим действительный IPv4 Address")
        networks = [ipaddress.ip_network(v.strip(), strict=False) for v in peer.get("AllowedIPs", "").split(",") if v.strip()]
        ipv4 = list(ipaddress.collapse_addresses(n for n in networks if n.version == 4))
        if ipv4 != [ipaddress.ip_network("0.0.0.0/0")]:
            raise SetupError("Профиль должен разрешать весь IPv4 внутри изолированного namespace")
        dns = [ipaddress.ip_address(v.strip()) for v in interface.get("DNS", "").split(",") if v.strip()]
        dns = [v for v in dns if v.version == 4]
        if not dns or any(v.is_loopback or v.is_unspecified or v.is_multicast for v in dns):
            raise SetupError("В профиле необходим IPv4 DNS; локальный stub DNS не подходит")
        mtu = int(interface.get("MTU", "1420"))
        keepalive = int(peer.get("PersistentKeepalive", "25"))
        if not 576 <= mtu <= 1420 or not 0 <= keepalive <= 65535:
            raise SetupError("Проверьте MTU (576–1420) и PersistentKeepalive")
    except SetupError:
        raise
    except (ValueError, configparser.Error, OSError):
        raise SetupError("Не удалось разобрать стандартный WireGuard-профиль; значения не выводятся") from None
    # wg setconf, unlike wg-quick, cannot change the host's routes or execute hooks.
    lines = ["[Interface]", "PrivateKey = " + interface["PrivateKey"], "", "[Peer]", "PublicKey = " + peer["PublicKey"]]
    if peer.get("PresharedKey"):
        lines.append("PresharedKey = " + peer["PresharedKey"])
    lines += ["AllowedIPs = 0.0.0.0/0", f"Endpoint = {expected}:{int(match[2])}", f"PersistentKeepalive = {keepalive}"]
    return {"wg_config": "\n".join(lines) + "\n", "addresses": list(map(str, addresses)),
            "dns": list(map(str, dns)), "mtu": mtu, "endpoint": str(expected), "port": int(match[2])}


def choose_subnet(routes, addresses):
    occupied = [ipaddress.ip_interface(a).network for a in addresses]
    for row in routes:
        destination = row.get("dst", "default")
        if destination != "default":
            network = ipaddress.ip_network(destination, strict=False)
            if network.version == 4 and network.prefixlen:
                occupied.append(network)
    for candidate in ("10.203.254.0/30", "172.31.250.0/30", "192.168.253.0/30"):
        network = ipaddress.ip_network(candidate)
        if not any(network.overlaps(other) for other in occupied):
            host, proxy = list(network.hosts())
            return {"subnet": str(network), "host": str(host), "proxy": str(proxy)}
    raise SetupError("Нет свободной служебной подсети; существующие маршруты не изменены")


def command(*args, check=True, capture=False):
    result = subprocess.run(args, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, text=True, check=False, timeout=30)
    if check and result.returncode:
        raise SetupError(f"Команда {args[0]} завершилась с кодом {result.returncode}; конфигурация не выводится")
    return result


def atomic_write(path, text, mode=0o600, uid=None, gid=None):
    path = Path(path)
    if path.is_symlink():
        raise SetupError("Символические ссылки в управляемой конфигурации запрещены")
    fd, temporary = tempfile.mkstemp(prefix=".paintbot-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            if uid is not None:
                os.fchown(stream.fileno(), uid, gid)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare(profile_path, endpoint):
    profile = Path(profile_path)
    if profile.is_symlink() or not profile.is_file():
        raise SetupError("Нужен обычный файл профиля")
    permissions = profile.stat()
    if permissions.st_uid != 0 or stat.S_IMODE(permissions.st_mode) & 0o077:
        raise SetupError("Профиль должен принадлежать root и иметь права 600")
    if (PRIVATE.exists() and (PRIVATE.is_symlink() or any(PRIVATE.iterdir()))) or PROXY_META.exists():
        raise SetupError("Конфигурация уже создана; используйте start/stop, не перезаписывайте профиль")
    data = parse_profile(profile.read_text(), endpoint)
    routes = json.loads(command("ip", "-j", "-4", "route", "show", "table", "all", capture=True).stdout)
    link = choose_subnet(routes, data["addresses"])
    if NS in [row.split()[0] for row in command("ip", "netns", "list", capture=True).stdout.splitlines() if row]:
        raise SetupError("Namespace с таким именем уже существует; он не изменён")
    for name in (HOST_LINK, NS_LINK, WG_LINK):
        if command("ip", "link", "show", "dev", name, check=False).returncode == 0:
            raise SetupError("Имя служебного интерфейса уже занято; он не изменён")
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(PRIVATE, 0o700)
    if PUBLIC.is_symlink():
        raise SetupError("Каталог namespace не должен быть символической ссылкой")
    PUBLIC.mkdir(mode=0o755, parents=True, exist_ok=True)
    if list(PUBLIC.iterdir()):
        raise SetupError("Каталог namespace содержит чужие настройки; он не перезаписан")
    atomic_write(PRIVATE / "wireguard.conf", data.pop("wg_config"))
    data["link"] = link
    atomic_write(PRIVATE / "metadata.json", json.dumps(data))
    atomic_write(PUBLIC / "resolv.conf", "".join("nameserver " + address + "\n" for address in data["dns"]), 0o644)
    atomic_write(PUBLIC / "nsswitch.conf", "passwd: files\ngroup: files\nhosts: files dns\nnetworks: files\nservices: files\nprotocols: files\n", 0o644)
    atomic_write(PROXY_META, json.dumps(link), 0o644)
    print("Профиль проверен и сохранён. Ключи не выводятся; маршруты пока не изменены")


def cleanup():
    command("ip", "netns", "del", NS, check=False)
    command("ip", "link", "del", HOST_LINK, check=False)
    command("ip", "link", "del", WG_LINK, check=False)
    MARKER.unlink(missing_ok=True)


def start():
    if MARKER.exists():
        cleanup()
    # Refuse unrelated interfaces instead of deleting them.
    for name in (HOST_LINK, NS_LINK, WG_LINK):
        if command("ip", "link", "show", "dev", name, check=False).returncode == 0:
            raise SetupError("Служебное имя занято; существующий интерфейс не изменён")
    names = [row.split()[0] for row in command("ip", "netns", "list", capture=True).stdout.splitlines() if row]
    if NS in names:
        raise SetupError("Существующий namespace без отметки владения не изменён")
    data = json.loads((PRIVATE / "metadata.json").read_text())
    link = data["link"]
    # Recheck routes after reboot: refuse a newly conflicting network.
    routes = json.loads(command("ip", "-j", "-4", "route", "show", "table", "all", capture=True).stdout)
    selected = ipaddress.ip_network(link["subnet"])
    for row in routes:
        dst = row.get("dst", "default")
        if dst != "default" and ipaddress.ip_network(dst, strict=False).prefixlen and selected.overlaps(ipaddress.ip_network(dst, strict=False)):
            raise SetupError("Служебная подсеть теперь конфликтует с маршрутом; запуск остановлен")
    command("ip", "netns", "add", NS)
    MARKER.parent.mkdir(mode=0o700, exist_ok=True)
    MARKER.touch(mode=0o600)
    try:
        command("ip", "link", "add", HOST_LINK, "type", "veth", "peer", "name", NS_LINK)
        command("ip", "link", "set", NS_LINK, "netns", NS)
        command("ip", "address", "add", link["host"] + "/30", "dev", HOST_LINK)
        command("ip", "link", "set", HOST_LINK, "up")
        # The WireGuard UDP socket is born in the host's original network namespace.
        command("ip", "link", "add", WG_LINK, "type", "wireguard")
        command("ip", "link", "set", WG_LINK, "netns", NS)
        inside = ("ip", "netns", "exec", NS)
        command(*inside, "ip", "link", "set", "lo", "up")
        command(*inside, "ip", "address", "add", link["proxy"] + "/30", "dev", NS_LINK)
        command(*inside, "ip", "link", "set", NS_LINK, "up")
        command(*inside, "wg", "setconf", WG_LINK, str(PRIVATE / "wireguard.conf"))
        for address in data["addresses"]:
            command(*inside, "ip", "address", "add", address, "dev", WG_LINK)
        command(*inside, "ip", "link", "set", WG_LINK, "mtu", str(data["mtu"]), "up")
        command(*inside, "ip", "route", "add", "default", "dev", WG_LINK)
    except BaseException:
        cleanup()
        raise
    print("Namespace создан; default route основного сервера не изменён")


def stop():
    if MARKER.exists():
        cleanup()
        print("Служебный namespace удалён; основная сеть не изменена")


def update_proxy_env(text, proxy):
    """Preserve bot token and unrelated configuration; replace only proxy keys."""
    lines = [line for line in text.splitlines() if not re.match(r"^\s*(?:export\s+)?(?:WB_PROXY_URL|OZON_PROXY_URL)\s*=", line)]
    lines += ["WB_PROXY_URL='" + proxy + "'", "OZON_PROXY_URL='" + proxy + "'"]
    return "\n".join(lines) + "\n"


def attach(env_path):
    path = Path(env_path)
    if path.is_symlink() or not path.is_file():
        raise SetupError("Нужен существующий обычный .env приложения")
    link = json.loads(PROXY_META.read_text())
    proxy = "http://" + link["proxy"] + ":3128"
    original = path.read_text()
    backup = PRIVATE / "previous-proxy-lines.json"
    if not backup.exists():
        lines = [line for line in original.splitlines() if re.match(r"^\s*(?:export\s+)?(?:WB_PROXY_URL|OZON_PROXY_URL)\s*=", line)]
        atomic_write(backup, json.dumps({"env": str(path.resolve()), "lines": lines}))
    info = path.stat()
    atomic_write(path, update_proxy_env(original, proxy), uid=info.st_uid, gid=info.st_gid)
    print("WB_PROXY_URL и OZON_PROXY_URL направлены в namespace; прочие настройки сохранены")


def detach():
    backup = PRIVATE / "previous-proxy-lines.json"
    if not backup.exists():
        raise SetupError("Сохранённые прежние настройки proxy не найдены")
    data = json.loads(backup.read_text())
    path = Path(data["env"])
    if path.is_symlink() or not path.is_file():
        raise SetupError("Исходный .env недоступен или заменён ссылкой")
    lines = [line for line in path.read_text().splitlines() if not re.match(r"^\s*(?:export\s+)?(?:WB_PROXY_URL|OZON_PROXY_URL)\s*=", line)]
    info = path.stat()
    atomic_write(path, "\n".join(lines + data["lines"]) + "\n", uid=info.st_uid, gid=info.st_gid)
    print("Прежние настройки proxy восстановлены; токен и остальные параметры не изменены")


def connect_target(header, source, allowed_source):
    if source != allowed_source:
        return None
    if len(header) > 16384:
        return None
    try:
        first = header.split(b"\r\n", 1)[0].decode("ascii")
        method, target, version = first.split()
        host, port = target.rsplit(":", 1)
        if method == "CONNECT" and version in ("HTTP/1.0", "HTTP/1.1") and port == "443" and host.lower() in DOMAINS:
            return host.lower()
    except (ValueError, UnicodeError):
        pass
    return None


def proxy_handler(allowed_source, connector=None):
    gate = asyncio.Semaphore(8)
    connector = connector or asyncio.open_connection

    async def handle(reader, writer):
        upstream = None
        admitted = False
        established = False
        try:
            source = writer.get_extra_info("peername")[0]
            if source != allowed_source or gate.locked():
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            await gate.acquire()
            admitted = True
            async with asyncio.timeout(10):
                header = await reader.readuntil(b"\r\n\r\n")
            host = connect_target(header, source, allowed_source)
            if not host:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            async with asyncio.timeout(20):
                up_reader, upstream = await connector(host, 443, family=socket.AF_INET)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            established = True

            async def pump(source_reader, target_writer):
                while chunk := await source_reader.read(65536):
                    target_writer.write(chunk)
                    await target_writer.drain()
                if target_writer.can_write_eof():
                    target_writer.write_eof()

            async with asyncio.timeout(180):
                await asyncio.gather(pump(reader, upstream), pump(up_reader, writer))
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            if not established:
                try:
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                except OSError:
                    pass
        finally:
            if upstream:
                upstream.close()
            writer.close()
            if admitted:
                gate.release()

    return handle


async def serve_proxy():
    data = json.loads(PROXY_META.read_text())
    server = await asyncio.start_server(proxy_handler(data["host"]), data["proxy"], 3128, limit=16384)
    async with server:
        await server.serve_forever()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "start", "stop", "proxy", "attach", "detach"])
    parser.add_argument("--profile")
    parser.add_argument("--endpoint", default="95.161.4.67")
    parser.add_argument("--env-file", default="/opt/paintbot/.env")
    args = parser.parse_args()
    try:
        if args.action == "proxy":
            asyncio.run(serve_proxy())
            return
        if os.geteuid() != 0:
            raise SetupError("Сетевые действия выполняются root на внешнем Ubuntu-сервере")
        if args.action == "prepare":
            if not args.profile:
                raise SetupError("Укажите локальный защищённый файл через --profile")
            prepare(args.profile, args.endpoint)
        elif args.action == "attach":
            attach(args.env_file)
        elif args.action == "detach":
            detach()
        elif args.action == "start":
            start()
        elif args.action == "stop":
            stop()
    except SetupError as exc:
        raise SystemExit(str(exc)) from None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        # Even ConfigParser/OS failures must not include original key material.
        raise SystemExit("VPN-операция не выполнена. Проверьте профиль, права, Endpoint, DNS и свободные интерфейсы; секреты не выводятся") from None


if __name__ == "__main__":
    main()
