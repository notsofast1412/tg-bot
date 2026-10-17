import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from aiogram import Bot
from aiogram.client.telegram import TelegramAPIServer

from paintbot.bot.session import TelegramSession
from paintbot.marketplaces import make_adapters
from paintbot.config import Config


@pytest.fixture
def local_api():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps({'ok': True, 'result': {'id': 999, 'is_bot': True,
                'first_name': 'Test', 'username': 'test_bot'}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_POST = do_GET

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


async def test_clients_ignore_environment_proxies(local_api, monkeypatch):
    # Invalid values would prevent client construction if HTTPX trusted them.
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.setenv(key, 'unsupported://unused.invalid:9999')
    monkeypatch.setenv('NO_PROXY', '')
    monkeypatch.setenv('no_proxy', '')
    adapters = make_adapters(gap=0)
    session = TelegramSession(api=TelegramAPIServer.from_base(local_api))
    bot = Bot('999:TEST_TOKEN', session=session)
    try:
        for adapter in adapters.values():
            assert (await adapter.request(local_api)).status_code == 200
        assert (await bot.get_me()).id == 999
    finally:
        await session.close()
        for adapter in adapters.values():
            await adapter.client.aclose()


def test_stale_proxy_settings_do_not_break_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_ID", raising=False)
    path = tmp_path / '.env'
    path.write_text('BOT_TOKEN=999:TEST_TOKEN\nOWNER_ID=1\n'
                    'WB_PROXY_URL=http://obsolete.invalid:3128\nOZON_PROXY_URL=invalid\n')
    assert Config(_env_file=path).owner_id == 1
    assert 'wb_proxy_url' not in Config.model_fields
    assert 'ozon_proxy_url' not in Config.model_fields


async def test_direct_clients_keep_tls_verification(monkeypatch):
    import httpx
    real_client = httpx.AsyncClient
    contexts = []

    def client(**kwargs):
        contexts.append(kwargs['verify'])
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    adapters = make_adapters()
    session = TelegramSession()
    try:
        assert len(contexts) == 3
        assert all(context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
                   for context in contexts)
    finally:
        await session.close()
        for adapter in adapters.values():
            await adapter.client.aclose()
