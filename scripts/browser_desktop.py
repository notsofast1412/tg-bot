"""Manual Ozon browser session, viewable only through a loopback VNC + SSH tunnel.

Run as paintbot inside xvfb-run, using the separate Playwright environment.
Never exports cookies or solves challenges automatically.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess

from playwright.async_api import async_playwright


async def main():
    os.umask(0o077)
    root = Path('/opt/paintbot')
    password = root / 'var/vnc.pass'
    if not password.is_file() or password.stat().st_mode & 0o077:
        raise RuntimeError('Create var/vnc.pass as paintbot, with permissions 600')
    if not os.environ.get('DISPLAY') or not os.environ.get('XAUTHORITY'):
        raise RuntimeError('Run this script through xvfb-run')
    if not shutil.which('x11vnc'):
        raise RuntimeError('Install x11vnc first')
    with socket.socket() as check:
        check.bind(('127.0.0.1', 5901))
    profile = root / 'var/browser-profile'
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile, 0o700)
    metadata = json.loads(Path('/etc/paintbot-ru-proxy.json').read_text())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(profile), channel='chromium', headless=False,
            chromium_sandbox=True, locale='ru-RU', viewport=None,
            proxy={'server': 'http://' + metadata['proxy'] + ':3128'})
        context.on('close', lambda *_: stop.set())
        try:
            with (root / 'var/browser-desktop.log').open('a') as log:
                vnc = subprocess.Popen([
                    'x11vnc', '-display', os.environ['DISPLAY'],
                    '-auth', os.environ['XAUTHORITY'],
                    '-localhost', '-listen', '127.0.0.1', '-rfbport', '5901',
                    '-rfbauth', str(password), '-forever', '-shared', '-quiet',
                ], stdout=log, stderr=log)
                try:
                    await asyncio.sleep(1)
                    if vnc.poll() is not None:
                        raise RuntimeError('VNC failed; inspect var/browser-desktop.log')
                    print('Desktop ready: SSH tunnel to 127.0.0.1:5901. Ctrl+C stops it.', flush=True)
                    page = context.pages[0] if context.pages else await context.new_page()
                    try:
                        await page.goto('https://www.ozon.ru/product/196525011/',
                                        wait_until='domcontentloaded', timeout=45000)
                    except Exception as exc:
                        print('Navigation:', type(exc).__name__, flush=True)
                    while not stop.is_set():
                        if vnc.poll() is not None:
                            raise RuntimeError('VNC stopped; inspect var/browser-desktop.log')
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=1)
                        except asyncio.TimeoutError:
                            pass
                finally:
                    if vnc.poll() is None:
                        vnc.terminate()
                        try:
                            await asyncio.to_thread(vnc.wait, timeout=5)
                        except subprocess.TimeoutExpired:
                            vnc.kill()
                            await asyncio.to_thread(vnc.wait)
        finally:
            await context.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc)[:1800])
        raise SystemExit(1)
