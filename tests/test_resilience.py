import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe, SendMessage

from paintbot.bot.session import TelegramSession
from paintbot.config import Config
from paintbot.marketplaces import OzonAdapter, WildberriesAdapter
from paintbot.marketplaces.links import MarketError, from_article
from paintbot.scheduler.monitor import Monitor


async def test_429_cooldown_does_not_sleep_or_block_other_markets(store, card, product_id):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "7200"})))
    adapter = WildberriesAdapter(client=client, gap=0)
    monitor = Monitor(store, {"wb": adapter})
    try:
        await monitor.cycle()
        assert int(store.setting("cooldown:wb")) >= int(time.time()) + 7190
        assert store.product(product_id).next_check >= int(time.time()) + 7190
        with pytest.raises(MarketError) as caught:
            await adapter.fetch(from_article("wb", "456"))
        assert caught.value.retry_after > 7100
        replacement = WildberriesAdapter(client=client, gap=0)
        Monitor(store, {"wb": replacement})
        assert replacement.blocked_until > time.time() + 7100
    finally:
        await client.aclose()


async def test_wb_block_does_not_try_fallback():
    calls = []
    def respond(r):
        calls.append(r.url.host)
        return httpx.Response(403)
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(MarketError):
            await WildberriesAdapter(client=client, gap=0).fetch(from_article("wb", "123"))
        assert calls == ["card.wb.ru"]
    finally:
        await client.aclose()


async def test_telegram_transport_serialization_and_api_response():
    calls = []
    def respond(r):
        calls.append(r)
        if r.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok":True,"result":{"id":999,"is_bot":True,"first_name":"Test"}})
        return httpx.Response(200, json={"ok":True,"result":{"message_id":1,"date":100,"chat":{"id":1,"type":"private"},"text":"hello"}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    session = TelegramSession(client=client)
    bot = Bot("999:TEST_TOKEN", session=session)
    try:
        assert (await bot.get_me()).id == 999
        assert (await bot.send_message(1,"hello")).message_id == 1
        assert b"chat_id=1" in calls[1].content
    finally:
        await bot.session.close()


async def test_transport_errors_never_echo_token():
    def respond(r):
        raise httpx.ConnectError(str(r.url))
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    bot = Bot("999:TEST_TOKEN", session=TelegramSession(client=client))
    try:
        with pytest.raises(TelegramNetworkError) as caught:
            await bot.get_me()
        assert "TEST_TOKEN" not in str(caught.value)
    finally:
        await bot.session.close()


def test_blank_optional_group_and_secret_configuration(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "999:TEST_TOKEN")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("GROUP_ID", "")
    config = Config(_env_file=None)
    assert config.group_id is None
    assert "TEST_TOKEN" not in repr(config)
