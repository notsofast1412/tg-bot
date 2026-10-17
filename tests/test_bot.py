import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramForbiddenError
from aiogram.methods import GetChat, GetChatMember, GetMe, SendMessage
from aiogram.types import Chat, ChatMemberMember, ChatPermissions, Message, Update, User

from paintbot.bot.access import Flows, OwnerOnly, authorized
from paintbot.bot.handlers import make_router
from paintbot.marketplaces.links import MarketError
from paintbot.services.batch import Batch, collect
from paintbot.services.diagnostics import diagnose
from paintbot.services.notifications import Sender
from paintbot.scheduler.monitor import Monitor


@pytest.mark.parametrize("user,chat,expected", [(1,"private",True), (2,"private",False), (1,"group",False), (1,"supergroup",False)])
def test_owner_access(user, chat, expected):
    assert authorized(user, chat, 1) == expected


def test_cancel_ttl_and_restart():
    flows = Flows(10)
    flows.start("links")
    flows.current.updated -= 11
    with pytest.raises(ValueError, match="истекло"):
        flows.get()
    assert flows.current is None
    flows.start("name")
    flows.cancel()
    assert flows.get() is None
    assert Flows(10).get() is None


async def test_partial_batch_duplicates_and_pending(store, card):
    adapter = AsyncMock()
    adapter.fetch.return_value = replace(card, price=None, warning="Нет цены")
    batch = Batch()
    link = card.url
    await collect(link + link + " https://evil.com/x", batch, {"wb": adapter}, store)
    assert len(batch.cards) == 1 and batch.skipped == 1 and len(batch.errors) == 1
    await collect("ошибка", batch, {"wb": adapter}, store)
    assert len(batch.errors) == 2
    store.add_batch(batch.cards, "краска", store.tags()[0].id)
    again = Batch()
    await collect(link, again, {"wb": adapter}, store)
    assert again.skipped == 1


async def test_batch_rejects_wrong_identity(store, card):
    adapter = AsyncMock()
    adapter.fetch.return_value = replace(card, card_id="456")
    batch = Batch()
    await collect(card.url, batch, {"wb": adapter}, store)
    assert not batch.cards and batch.errors


async def test_sender_restart_and_reprocessing(store, card, product_id):
    store.setting("group_id", -100123)
    store.record(product_id, replace(card, price=39000), None, 600)
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=123)
    sender = Sender(store, bot)
    assert await sender.deliver_one()
    assert not await sender.deliver_one()
    bot.send_message.assert_awaited_once()
    assert not store.queue()


async def test_definite_rate_limit_is_retried(store, card, product_id):
    store.setting("group_id", -100123)
    store.record(product_id, replace(card, price=39000), None, 600)
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramRetryAfter(method=SendMessage(chat_id=-100123,text="x"), message="limit", retry_after=1)
    sender = Sender(store, bot)
    await sender.deliver_one()
    q = store.queue()[0]
    assert q.state == "pending" and q.attempts == 1
    store.finish_notification(q.id, "pending")
    bot.send_message.side_effect = None
    bot.send_message.return_value = SimpleNamespace(message_id=10)
    assert await sender.deliver_one()
    assert not store.queue()


async def test_unknown_delivery_requires_explicit_retry(store, card, product_id):
    store.setting("group_id", -100123)
    store.record(product_id, replace(card, price=39000), None, 600)
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramNetworkError(method=SendMessage(chat_id=-100123,text="x"), message="timeout")
    sender = Sender(store, bot)
    await sender.deliver_one()
    assert store.queue()[0].state == "uncertain"
    assert not await sender.deliver_one()
    store.retry_notification(store.queue()[0].id)
    assert store.queue()[0].state == "pending"


def test_crash_in_flight_is_not_automatically_resent(store, card, product_id):
    store.record(product_id, replace(card, price=39000), None, 600)
    q = store.claim_notification(int(time.time()))
    assert q.state == "sending"
    store.recover()
    assert store.queue()[0].state == "uncertain"
    assert store.claim_notification(int(time.time())) is None


async def test_missing_group_preserves_queue(store, card, product_id):
    store.record(product_id, replace(card, price=39000), None, 600)
    bot = AsyncMock()
    assert not await Sender(store, bot).deliver_one()
    bot.send_message.assert_not_awaited()
    assert store.queue()[0].state == "pending"


async def test_diagnostics_calls_real_methods_via_mock_transport(store):
    bot = AsyncMock()
    bot.get_me.return_value = SimpleNamespace(id=999)
    bot.get_chat.return_value = SimpleNamespace(type="supergroup", permissions=SimpleNamespace(can_send_messages=True))
    bot.get_chat_member.return_value = SimpleNamespace(status="member")
    store.setting("group_id", -100123)
    monitor = Monitor(store, {})
    result = await diagnose(bot, store, monitor)
    bot.get_me.assert_awaited()
    bot.get_chat.assert_awaited_once_with(-100123)
    bot.get_chat_member.assert_awaited_once_with(-100123, 999)
    assert "право отправки: да" in result
    assert "не проверялся" in result
    assert "не запущен" in result


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return User(id=999, is_bot=True, first_name="Bot", username="testbot")
        if isinstance(method, GetChat):
            return SimpleNamespace(type="supergroup", permissions=SimpleNamespace(can_send_messages=True))
        if isinstance(method, GetChatMember):
            return SimpleNamespace(status="member")
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls), date=int(time.time()), chat=Chat(id=int(method.chat_id), type="private"), text=method.text)
        return True


@pytest.fixture
def telegram(store, card):
    session = RecordingSession()
    bot = Bot("999:TEST_TOKEN_WITHOUT_REAL_ACCESS", session=session)
    dp = Dispatcher()
    adapter = AsyncMock()
    adapter.fetch.return_value = card
    monitor = Monitor(store, {"wb": adapter})
    dp.update.outer_middleware(OwnerOnly(1, store))
    dp.include_router(make_router(store, {"wb": adapter}, monitor, bot))
    counter = 0

    async def feed(text=None, callback=None, user=1, chat="private"):
        nonlocal counter
        counter += 1
        message = {"message_id": counter, "date": int(time.time()), "chat": {"id": user if chat == "private" else -100123,"type": chat},
                   "from": {"id": user,"is_bot":False,"first_name":"User"}, "text": text or "button"}
        data = {"update_id": counter}
        if callback:
            data["callback_query"] = {"id":str(counter),"from":message["from"],"chat_instance":"x","data":callback,"message":message}
        else:
            data["message"] = message
        await dp.feed_update(bot, Update.model_validate(data))

    def button(prefix):
        for method in reversed(session.calls):
            markup = getattr(method, "reply_markup", None)
            for row in getattr(markup, "inline_keyboard", []):
                for b in row:
                    if b.callback_data and b.callback_data.startswith(prefix):
                        return b.callback_data
        raise AssertionError(f"button missing: {prefix}")

    return feed, button, session, bot


async def test_full_confirmed_batch_flow(telegram, store, card):
    feed, button, session, _ = telegram
    await feed("➕ Добавить товар")
    await feed(callback=button("method:links"))
    await feed(card.url + card.url)
    await feed(callback=button("batchdone"))
    await feed("Краска для работы")
    await feed("1: 2 баллона по 400 мл")
    await feed(callback=button("detailsdone"))
    await feed(callback=button("selecttag:"))
    assert store.products()[1] == 0  # no persistence before confirmation
    save = button("save")
    await feed(callback=save)
    assert store.products()[1] == 1
    await feed(callback=save)
    assert store.products()[1] == 1
    product = store.products()[0][0][0]
    assert "Уточнение владельца" in product.attributes


async def test_article_flow_requires_market_and_confirmation(telegram, store):
    feed, button, _, _ = telegram
    await feed("➕ Добавить товар")
    await feed(callback=button("method:article"))
    await feed(callback=button("market:wb"))
    await feed("123")
    assert store.products()[1] == 0
    await feed(callback=button("articleyes"))
    await feed("Краска")
    await feed(callback=button("detailsdone"))
    await feed(callback=button("selecttag:"))
    await feed(callback=button("save"))
    assert store.products()[1] == 1


async def test_unauthorized_messages_and_callbacks_ignored(telegram, store):
    feed, _, session, _ = telegram
    await feed("/tags", user=2)
    await feed("/setgroup", chat="supergroup")
    await feed(callback="edittag:1", user=2)
    assert not any(isinstance(m, SendMessage) for m in session.calls)
    assert not store.setting("group_id")


async def test_real_test_send_method_does_not_mutate_prices(telegram, store):
    feed, _, session, _ = telegram
    store.setting("group_id", -100123)
    await feed("📨 Проверить отправку в группу")
    assert any(isinstance(m, SendMessage) and m.chat_id == -100123 for m in session.calls)
    assert store.statistics()["queue"] == 0
    assert store.statistics()["last_attempt"] is None


async def test_stale_scenario_button_rejected(telegram, store):
    feed, button, session, _ = telegram
    await feed("➕ Добавить товар")
    old = button("method:links")
    await feed("/cancel")
    await feed("➕ Добавить товар")
    await feed(callback=old)
    assert "другому" in session.calls[-1].text


async def test_filters_history_status_handlers(telegram, store, card, product_id):
    feed, _, session, _ = telegram
    store.record(product_id, card, None, 600)
    for text in ("/list market=wb tag=#arton status=active", f"/history {product_id}", "/status", "/tags", "/settings", "/help"):
        await feed(text)
    assert any(isinstance(m, SendMessage) and "Изменения цены" in m.text for m in session.calls)


async def test_article_menu_only_offers_supported_marketplaces(telegram, store):
    feed, button, session, _ = telegram
    await feed("➕ Добавить товар")
    await feed(callback=button("method:article"))
    menu = next(m for m in reversed(session.calls) if isinstance(m, SendMessage))
    labels = [b.text for row in menu.reply_markup.inline_keyboard for b in row]
    assert labels == ["Wildberries", "Ozon", "Отмена"]
    # Old Telegram messages may still contain a removed marketplace callback.
    await feed(callback=button("market:wb").replace("market:wb", "market:yandex"))
    assert "устарела" in session.calls[-1].text
    assert store.products()[1] == 0
    await feed(callback=button("market:ozon"))
    assert "числовой ID" in session.calls[-1].text
