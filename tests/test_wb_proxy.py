import os

import httpx
import pytest

from paintbot.config import Config
from paintbot.marketplaces.adapters import make_adapters, validate_wb_proxy
from paintbot.marketplaces.links import MarketError, from_article


@pytest.mark.parametrize("value", [None, "", "  "])
def test_optional_proxy_defaults_to_existing_route(value):
    assert validate_wb_proxy(value) is None


@pytest.mark.parametrize("value", ["socks5://host:1080", "http://host", "http://host:bad",
    "https://host:443/private", "https://host:443?token=secret", "http://user:password@:80"])
def test_invalid_proxy_does_not_expose_credentials(value):
    with pytest.raises(MarketError) as caught:
        validate_wb_proxy(value)
    assert "password" not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert value not in str(caught.value)


async def test_explicit_proxy_is_used_only_for_wb_and_does_not_mutate_platform_proxy(monkeypatch):
    from paintbot.marketplaces import base
    original_client = httpx.AsyncClient
    seen = []
    before = os.environ.get("HTTPS_PROXY")
    def make_client(**kwargs):
        seen.append(dict(kwargs))
        kwargs.pop("proxy", None)
        return original_client(transport=httpx.MockTransport(lambda r: httpx.Response(403)), **kwargs)
    monkeypatch.setattr(base.httpx, "AsyncClient", make_client)
    adapters = make_adapters(wb_proxy_url="http://test-user:test-pass@ru-proxy.example:8080")
    try:
        assert seen[0]["proxy"] == "http://test-user:test-pass@ru-proxy.example:8080"
        assert seen[1]["proxy"] is None and seen[2]["proxy"] is None
        assert os.environ.get("HTTPS_PROXY") == before
        with pytest.raises(MarketError) as caught:
            await adapters["wb"].fetch(from_article("wb", "29220652"))
        assert "403" in str(caught.value)
        assert "test-pass" not in str(caught.value)
    finally:
        for adapter in adapters.values():
            await adapter.client.aclose()


def test_proxy_is_a_secret_and_empty_value_is_optional(monkeypatch):
    monkeypatch.delenv("WB_PROXY_URL", raising=False)
    config = Config(_env_file=None, bot_token="999:TEST_TOKEN", owner_id=1,
                    wb_proxy_url="https://test-user:test-pass@proxy.example:443")
    assert "test-pass" not in repr(config)
    assert Config(_env_file=None, bot_token="999:TEST_TOKEN", owner_id=1, wb_proxy_url="").wb_proxy_url is None
