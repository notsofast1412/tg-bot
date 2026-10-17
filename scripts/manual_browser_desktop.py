"""Launch Chromium directly for manual inspection through loopback VNC.

No Playwright, WebDriver, remote debugging, or automatic challenge interaction.
Run as paintbot under xvfb-run. The browser keeps its normal sandbox enabled.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit


DEFAULT_URL = 'https://www.ozon.ru/product/197922669/'


def browser_path(root, requested=None):
    if requested:
        executable = shutil.which(requested)
        if not executable:
            raise RuntimeError('Requested browser is not installed or executable')
        return Path(executable)
    for name in ('google-chrome-stable', 'google-chrome', 'chromium', 'chromium-browser'):
        executable = shutil.which(name)
        if executable:
            return Path(executable)
    managed = root / 'var/chromium/chromium-1187/chrome-linux/chrome'
    if managed.is_file() and os.access(managed, os.X_OK):
        return managed
    raise RuntimeError('Install a supported browser or specify --browser /path/to/browser')


def card_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port:
            raise ValueError()
        if parsed.hostname in ('ozon.ru', 'www.ozon.ru'):
            if not parsed.path.startswith('/product/'):
                raise ValueError()
        elif parsed.hostname in ('wildberries.ru', 'www.wildberries.ru'):
            sizes = parse_qs(parsed.query, keep_blank_values=True).get('size', [])
            if (not re.fullmatch(r'/catalog/[1-9]\d*/detail\.aspx/?', parsed.path)
                    or (sizes and (len(sizes) != 1 or not re.fullmatch(r'[1-9]\d*', sizes[0])))):
                raise ValueError()
        else:
            raise ValueError()
    except ValueError:
        raise argparse.ArgumentTypeError('Use an HTTPS WB/Ozon product URL without credentials or port') from None
    return value


def main(browser=None, url=DEFAULT_URL):
    os.umask(0o077)
    root = Path('/opt/paintbot')
    password = root / 'var/vnc.pass'
    chrome = browser_path(root, browser)
    if not password.is_file() or password.stat().st_mode & 0o077:
        raise RuntimeError('Create var/vnc.pass with permissions 600 first')
    display, authority = os.environ.get('DISPLAY'), os.environ.get('XAUTHORITY')
    if not display or not authority:
        raise RuntimeError('Run through xvfb-run')
    with socket.socket() as check:
        check.bind(('127.0.0.1', 5901))
    managed = root / 'var/chromium/chromium-1187/chrome-linux/chrome'
    profile = root / ('var/manual-browser-profile' if chrome == managed else 'var/manual-browser-stable-profile')
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile, 0o700)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    children = []
    try:
        version = subprocess.run([str(chrome), '--version'], check=True, capture_output=True,
                                 text=True, timeout=10)
        print('Browser:', version.stdout.strip(), flush=True)
        with (root / 'var/manual-browser-desktop.log').open('a') as log:
            vnc = subprocess.Popen([
                'x11vnc', '-display', display, '-auth', authority,
                '-localhost', '-listen', '127.0.0.1', '-rfbport', '5901',
                '-rfbauth', str(password), '-forever', '-shared', '-quiet',
            ], stdout=log, stderr=log)
            children.append(vnc)
            if stop.wait(1) or vnc.poll() is not None:
                raise RuntimeError('VNC startup failed; inspect var/manual-browser-desktop.log')
            browser = subprocess.Popen([
                str(chrome), '--user-data-dir=' + str(profile),
                '--lang=ru-RU', '--no-first-run',
                '--no-proxy-server',
                url,
            ], stdout=log, stderr=log)
            children.append(browser)
            print('Direct Chromium launch. Connect through the existing SSH/VNC tunnel.', flush=True)
            print('Ctrl+C stops this desktop. Profile stays in', str(profile), flush=True)
            while not stop.wait(1):
                if vnc.poll() is not None:
                    raise RuntimeError('VNC stopped; inspect var/manual-browser-desktop.log')
                if browser.poll() is not None:
                    if browser.returncode:
                        raise RuntimeError('Chromium failed; inspect var/manual-browser-desktop.log')
                    break
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', help='Installed browser command or executable path')
    parser.add_argument('--url', type=card_url, default=DEFAULT_URL)
    args = parser.parse_args()
    try:
        main(args.browser, args.url)
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc)[:1200])
        raise SystemExit(1)
