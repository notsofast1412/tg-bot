"""Authenticated loopback job queue and an ordinary, persistent Chrome desktop."""
import asyncio
import hmac
import math
import os
import re
import secrets
import signal
import subprocess
import time

from aiohttp import web

from paintbot.marketplaces.links import MarketError, parse_link
from paintbot.marketplaces.wb_browser import KEY_PATH, ROOT, SOURCE, public_product


class BridgeError(Exception):
    pass


class Broker:
    def __init__(self, timeout=75):
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self.active = None
        self.last_poll = 0

    async def fetch(self, url):
        ref = parse_link(url)
        if ref.marketplace != 'wb' or ref.url != url:
            raise BridgeError('invalid_result')
        if time.monotonic() - self.last_poll > 6:
            raise BridgeError('extension_missing')
        if self.lock.locked():
            raise BridgeError('busy')
        async with self.lock:
            future = asyncio.get_running_loop().create_future()
            self.active = {'id': secrets.token_hex(16), 'url': url,
                           'issued_at': time.time(), 'future': future, 'session': None}
            try:
                return await asyncio.wait_for(future, self.timeout)
            except asyncio.TimeoutError:
                raise BridgeError('timeout') from None
            finally:
                self.active = None
                if not future.done():
                    future.cancel()

    def claim(self, session):
        if not isinstance(session, str) or not re.fullmatch(r'[a-f0-9-]{32,40}', session):
            raise BridgeError('invalid_result')
        self.last_poll = time.monotonic()
        if self.active and self.active['session'] is None:
            self.active['session'] = session
            return {k: self.active[k] for k in ('id', 'url', 'issued_at')}
        return None

    def submit(self, result):
        job = self.active
        if (not isinstance(result, dict) or not job or job['future'].done()
                or result.get('job_id') != job['id'] or result.get('session') != job['session']
                or job['session'] is None):
            raise BridgeError('invalid_result')
        started = result.get('started_at')
        if (type(started) not in (int, float) or not math.isfinite(started)
                or not job['issued_at'] <= started <= time.time() + 2):
            raise BridgeError('invalid_result')
        ref = parse_link(job['url'])
        if result.get('page_url') != ref.url:
            raise BridgeError('invalid_result')
        status = result.get('status')
        if type(status) is not int:
            raise BridgeError('invalid_result')
        if result.get('kind') == 'document':
            if status >= 400:
                job['future'].set_exception(BridgeError('rate_limit' if status == 429 else 'page_refused'))
                return
            raise BridgeError('invalid_result')
        if result.get('source') != SOURCE:
            raise BridgeError('invalid_result')
        if status != 200:
            job['future'].set_exception(BridgeError('rate_limit' if status == 429 else 'api_refused'))
            return
        age = result.get('age_seconds')
        if type(age) not in (int, float) or not math.isfinite(age) or not 0 <= age <= 120:
            raise BridgeError('invalid_result')
        products = result.get('products')
        if not isinstance(products, list) or not 1 <= len(products) <= 10:
            raise BridgeError('invalid_result')
        nm = ref.card_id.split('|')[0]
        try:
            products = [public_product({'products': [product]}, nm) for product in products]
        except MarketError:
            raise BridgeError('invalid_result') from None
        dest = result.get('dest')
        if not isinstance(dest, str) or not re.fullmatch(r'-?\d{1,12}', dest):
            dest = None
        job['future'].set_result({'schema': 1, 'url': ref.url, 'products': products, 'dest': dest})


def application(key, broker=None):
    broker = broker or Broker()

    @web.middleware
    async def authenticate(request, handler):
        port = request.transport.get_extra_info('sockname')[1]
        if request.headers.get('Host') != f'127.0.0.1:{port}':
            return web.json_response({'error': 'unauthorized'}, status=403)
        origin = request.headers.get('Origin', '')
        extension_origin = re.fullmatch(r'chrome-extension://[a-p]{32}', origin)
        cors = {'Access-Control-Allow-Origin': origin, 'Vary': 'Origin'} if extension_origin else {}
        if request.method == 'OPTIONS':
            if not extension_origin or request.path not in ('/fetch', '/job', '/result', '/health'):
                return web.Response(status=403)
            return web.Response(status=204, headers={**cors,
                'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                'Access-Control-Allow-Headers': 'Authorization, Content-Type',
                'Access-Control-Allow-Private-Network': 'true'})
        if not hmac.compare_digest(request.headers.get('Authorization', '').encode(), ('Bearer ' + key).encode()):
            return web.json_response({'error': 'unauthorized'}, status=403, headers=cors)
        try:
            response = await handler(request)
        except (BridgeError, MarketError) as exc:
            code = str(exc) if isinstance(exc, BridgeError) else 'invalid_result'
            response = web.json_response({'error': code}, status=503)
        except (ValueError, TypeError):
            response = web.json_response({'error': 'invalid_result'}, status=400)
        response.headers.update(cors)
        return response

    app = web.Application(middlewares=[authenticate], client_max_size=200_000)

    async def fetch(request):
        payload = await request.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('url'), str):
            raise BridgeError('invalid_result')
        return web.json_response(await broker.fetch(payload['url']))

    async def claim(request):
        return web.json_response({'job': broker.claim(request.query.get('session'))})

    async def submit(request):
        broker.submit(await request.json())
        return web.json_response({'ok': True})

    async def health(request):
        return web.json_response({'extension_connected': time.monotonic() - broker.last_poll < 6,
                                  'busy': broker.active is not None})

    app.router.add_post('/fetch', fetch)
    app.router.add_get('/job', claim)
    app.router.add_post('/result', submit)
    app.router.add_get('/health', health)
    app.router.add_options('/{tail:.*}', health)
    return app


async def desktop():
    key = KEY_PATH.read_text().strip()
    if not re.fullmatch(r'[a-f0-9]{64}', key):
        raise RuntimeError('Run scripts/setup_wb_browser.sh first')
    password = ROOT / 'var/vnc.pass'
    if not password.is_file() or password.stat().st_mode & 0o077:
        raise RuntimeError('VNC password file must exist with permissions 600')
    display, authority = os.environ.get('DISPLAY'), os.environ.get('XAUTHORITY')
    if not display or not authority:
        raise RuntimeError('Run this desktop through xvfb-run')
    broker = Broker()
    runner = web.AppRunner(application(key, broker), access_log=None,
                           handler_cancellation=True, shutdown_timeout=2)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    children = []
    environment = {k: v for k, v in os.environ.items() if k not in ('BOT_TOKEN', 'OWNER_ID', 'GROUP_ID')}
    try:
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', 8765).start()
        with (ROOT / 'var/wb-desktop.log').open('a') as log:
            vnc = subprocess.Popen(['x11vnc', '-display', display, '-auth', authority,
                '-localhost', '-listen', '127.0.0.1', '-rfbport', '5901', '-rfbauth', str(password),
                '-forever', '-shared', '-quiet'], stdout=log, stderr=log, env=environment)
            children.append(vnc)
            await asyncio.sleep(1)
            if vnc.poll() is not None:
                raise RuntimeError('VNC could not start; close the previous manual desktop')
            chrome = subprocess.Popen(['/usr/bin/google-chrome-stable',
                '--user-data-dir=' + str(ROOT / 'var/manual-browser-stable-profile'),
                '--lang=ru-RU', '--no-first-run', '--no-proxy-server',
                '--auto-open-devtools-for-tabs',
                'https://www.wildberries.ru/catalog/518397236/detail.aspx?size=716573024'],
                stdout=log, stderr=log, env=environment)
            children.append(chrome)
            while not stop.is_set():
                if any(child.poll() is not None for child in children):
                    raise RuntimeError('Chrome/VNC stopped; close other browsers using this profile')
                try:
                    await asyncio.wait_for(stop.wait(), 1)
                except asyncio.TimeoutError:
                    pass
    finally:
        if broker.active and not broker.active['future'].done():
            broker.active['future'].set_exception(BridgeError('browser_stopped'))
        await runner.cleanup()
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    await asyncio.to_thread(child.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait)


def main():
    os.umask(0o077)
    try:
        asyncio.run(desktop())
    except Exception as exc:
        # Configuration, credentials and response bodies are never rendered here.
        raise SystemExit('WB desktop stopped: ' + type(exc).__name__) from None


if __name__ == '__main__':
    main()
