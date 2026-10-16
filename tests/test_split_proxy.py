import os
from unittest.mock import AsyncMock

import httpx
import pytest

from paintbot.config import Config
from paintbot.marketplaces.adapters import make_adapters
from paintbot.marketplaces.links import MarketError


async def test_both_marketplaces_can_share_russian_gateway_without_rerouting_yandex(monkeypatch):
    from paintbot.marketplaces import base
    original_client = httpx.AsyncClient
    created, requests = [], []
    before = {key: os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
    def client(**kwargs):
        proxy = kwargs.pop("proxy", None)
        created.append(proxy)
        def respond(request):
            requests.append((proxy, str(request.url)))
            return httpx.Response(403)
        return original_client(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(base.httpx, "AsyncClient", client)
    proxy = "http://10.99.0.1:3128"
    adapters = make_adapters(gap=0, wb_proxy_url=proxy, ozon_proxy_url=proxy)
    try:
        assert created == [proxy, proxy, None]
        for market, url in (("wb", "https://card.wb.ru/"), ("ozon", "https://ozon.ru/t/8M08OsF")):
            with pytest.raises(MarketError):
                await adapters[market].request(url)
        assert all(route == proxy for route, _ in requests)
        assert before == {key: os.environ.get(key) for key in before}
    finally:
        for adapter in adapters.values():
            await adapter.client.aclose()


async def test_ozon_proxy_failure_has_no_direct_fallback_and_no_credential_echo(monkeypatch):
    from paintbot.marketplaces import base
    original_client = httpx.AsyncClient
    calls = []
    def client(**kwargs):
        proxy = kwargs.pop("proxy", None)
        def respond(request):
            calls.append((proxy, str(request.url)))
            raise httpx.ProxyError("http://test-user:SECRET_PASS@gateway.example:3128")
        return original_client(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(base.httpx, "AsyncClient", client)
    monkeypatch.setattr(base.asyncio, "sleep", AsyncMock())
    proxy = "http://test-user:SECRET_PASS@gateway.example:3128"
    adapters = make_adapters(gap=0, ozon_proxy_url=proxy)
    try:
        with pytest.raises(MarketError) as caught:
            await adapters["ozon"].resolve_short("https://ozon.ru/t/8M08OsF")
        assert len(calls) == 2 and all(route == proxy for route, _ in calls)
        assert "SECRET_PASS" not in str(caught.value)
        assert "gateway.example" not in str(caught.value)
    finally:
        for adapter in adapters.values():
            await adapter.client.aclose()


def test_ozon_proxy_is_secret_and_optional(monkeypatch):
    monkeypatch.delenv("OZON_PROXY_URL", raising=False)
    config = Config(_env_file=None, bot_token="999:TEST_TOKEN", owner_id=1,
                    ozon_proxy_url="http://test-user:SECRET_PASS@host.example:3128")
    assert "SECRET_PASS" not in repr(config)
    assert Config(_env_file=None, bot_token="999:TEST_TOKEN", owner_id=1, ozon_proxy_url="").ozon_proxy_url is None


def test_invalid_ozon_proxy_is_rejected_before_constructing_any_client(monkeypatch):
    from paintbot.marketplaces import base
    def unexpected(**kwargs):
        pytest.fail("No client should be created when validation fails")
    monkeypatch.setattr(base.httpx, "AsyncClient", unexpected)
    with pytest.raises(MarketError, match="OZON_PROXY_URL") as caught:
        make_adapters(ozon_proxy_url="http://test-user:SECRET_PASS@host.example:invalid")
    assert "SECRET_PASS" not in str(caught.value)
