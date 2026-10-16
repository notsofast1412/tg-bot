import asyncio
import os
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import GetWebhookInfo, SendMessage
from aiogram.types import WebhookInfo
from alembic import command
from alembic.config import Config as AlembicConfig

from paintbot.config import Config
from tests.test_bot import RecordingSession, telegram


def test_schema_matches_models_and_repeated_upgrade(store):
    config = AlembicConfig("alembic.ini")
    command.upgrade(config, "head")
    command.check(config)


async def test_application_lifecycle_price_to_group_with_fake_transport(store, card, product_id, monkeypatch):
    """No network: genuine application tasks and DB, fake marketplace/Telegram."""
    from paintbot import __main__ as app
    delivered = asyncio.Event()

    class Session(RecordingSession):
        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, GetWebhookInfo):
                return WebhookInfo(url="", has_custom_certificate=False, pending_update_count=0)
            result = await super().make_request(bot, method, timeout)
            if isinstance(method, SendMessage) and method.chat_id == -100123:
                delivered.set()
            return result

    session = Session()
    bot = Bot("999:TEST_TOKEN", session=session)
    adapter = AsyncMock()
    adapter.fetch.return_value = replace(card, price=38900)
    monkeypatch.setattr(app, "Bot", lambda *args, **kwargs: bot)
    # Do not instantiate an unused real transport through keyword evaluation.
    monkeypatch.setattr(app, "ProxyAwareSession", lambda: session)
    monkeypatch.setattr(app, "make_adapters", lambda gap, **kwargs: {"wb": adapter})

    async def poll(*args, **kwargs):
        await asyncio.wait_for(delivered.wait(), timeout=5)

    monkeypatch.setattr(app.Dispatcher, "start_polling", poll)
    config = Config(_env_file=None, bot_token="999:TEST_TOKEN", owner_id=1,
                    group_id=-100123, database_path=store.path)
    await app.run(config)
    assert store.product(product_id).last_price == 38900
    assert not store.queue()
    adapter.client.aclose.assert_awaited_once()
    sends = [m for m in session.calls if isinstance(m, SendMessage)]
    assert len(sends) == 1
    assert "Цена ниже порога" in sends[0].text


async def test_group_configuration_and_cancel_through_dispatcher(telegram, store):
    feed, button, session, bot = telegram
    await feed("/setgroup")
    await feed("-100123")
    assert store.setting("group_id") == "-100123"
    await feed("➕ Добавить товар")
    await feed(callback=button("method:links"))
    await feed("/cancel")
    await feed("https://www.wildberries.ru/catalog/123/detail.aspx")
    assert store.products()[1] == 0
