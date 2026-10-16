"""Launch Chromium directly for manual inspection through loopback VNC.

No Playwright, WebDriver, remote debugging, or automatic challenge interaction.
Run as paintbot under xvfb-run. The browser keeps its normal sandbox enabled.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading


def main(direct=False):
    os.umask(0o077)
    root = Path('/opt/paintbot')
    password = root / 'var/vnc.pass'
    chrome = root / 'var/chromium/chromium-1187/chrome-linux/chrome'
    if not chrome.is_file():
        raise RuntimeError('Expected Chromium build 1187 is not installed')
    if not password.is_file() or password.stat().st_mode & 0o077:
        raise RuntimeError('Create var/vnc.pass with permissions 600 first')
    display, authority = os.environ.get('DISPLAY'), os.environ.get('XAUTHORITY')
    if not display or not authority:
        raise RuntimeError('Run through xvfb-run')
    with socket.socket() as check:
        check.bind(('127.0.0.1', 5901))
    profile = root / 'var/manual-browser-profile'
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile, 0o700)
    metadata = json.loads(Path('/etc/paintbot-ru-proxy.json').read_text())
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    children = []
    try:
        with (root / 'var/manual-browser-desktop.log').open('a') as log:
            vnc = subprocess.Popen([
                'x11vnc', '-display', display, '-auth', authority,
                '-localhost', '-listen', '127.0.0.1', '-rfbport', '5901',
                '-rfbauth', str(password), '-forever', '-shared', '-quiet',
            ], stdout=log, stderr=log)
            children.append(vnc)
            if stop.wait(1) or vnc.poll() is not None:
                raise RuntimeError('VNC startup failed; inspect var/manual-browser-desktop.log')
            proxy_options = [] if direct else [
                '--proxy-server=http://' + metadata['proxy'] + ':3128']
            browser = subprocess.Popen([
                str(chrome), '--user-data-dir=' + str(profile),
                '--lang=ru-RU', '--no-first-run',
                *proxy_options,
                'https://www.ozon.ru/product/196525011/',
            ], stdout=log, stderr=log)
            children.append(browser)
            print('Direct Chromium launch. Connect through the existing SSH/VNC tunnel.', flush=True)
            print('Network:', 'host routing (no explicit proxy)' if direct else 'configured VPN proxy', flush=True)
            print('Ctrl+C stops this desktop. Profile stays in var/manual-browser-profile.', flush=True)
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
    parser.add_argument('--direct', action='store_true',
                        help='Use host routing without the CONNECT proxy (for the timed full-VPN test)')
    args = parser.parse_args()
    try:
        main(args.direct)
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc)[:1200])
        raise SystemExit(1)
