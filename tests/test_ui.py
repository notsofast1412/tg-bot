from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import GetChat
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select

from paintwatch.bot.ui import OwnerGate, UI
from paintwatch.database.models import Notification
from paintwatch.diagnostics import group_access, test_group as send_test_group
from paintwatch.services import Monitor


def message(text, user_id=42, chat_type="private", chat_id=42):
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="Owner"),
        text=text,
    )


def callback(data, user_id=42, chat_type="private"):
    return CallbackQuery(
        id="callback",
        from_user=User(id=user_id, is_bot=False, first_name="Owner"),
        chat_instance="instance",
        message=message("button", user_id, chat_type),
        data=data,
    )


@pytest.fixture
def ui(store, config, fake_adapters):
    bot = AsyncMock()
    monitor = Monitor(store, fake_adapters, config)
    return UI(store, fake_adapters, config, monitor, bot)


@pytest.mark.parametrize(
    "user_id,chat_type",
    [(99, "private"), (42, "group"), (42, "supergroup"), (99, "supergroup")],
)
@pytest.mark.parametrize("kind", ["message", "callback"])
async def test_owner_private_gate_every_event(store, user_id, chat_type, kind):
    gate = OwnerGate(42, store)
    handler = AsyncMock()
    obj = (
        Update(update_id=1, message=message("/setgroup -100123", user_id, chat_type))
        if kind == "message"
        else Update(
            update_id=1, callback_query=callback("save:nonce", user_id, chat_type)
        )
    )
    await gate(handler, obj, {"bot": AsyncMock()})
    handler.assert_not_called()


async def test_duplicate_updates_ignored(store):
    gate = OwnerGate(42, store)
    handler = AsyncMock()
    event = Update(update_id=7, message=message("/start"))
    await gate(handler, event, {})
    await gate(handler, event, {})
    handler.assert_awaited_once()


async def click(ui, data):
    q = SimpleNamespace(data=data, answer=AsyncMock())
    await ui.callback(q)


async def test_complete_batch_flow_and_confirmation(ui, store):
    await ui.message(message("➕ Добавить товар"))
    await click(ui, "add:link")
    await ui.message(
        message(
            "https://wildberries.ru/catalog/123/detail.aspxhttps://ozon.ru/product/456/"
        )
    )
    assert store.products()[1] == 0
    await click(ui, "batch:next")
    await ui.message(message("Краска для работ"))
    await click(ui, "choose:1")
    await ui.message(message("/default"))
    await click(ui, "meta:skip")
    draft = store.draft(42)
    assert draft["step"] == "confirm" and store.products()[1] == 0
    await click(ui, "save:" + draft["nonce"])
    assert store.products()[1] == 2
    assert store.draft(42) is None
    await click(ui, "save:" + draft["nonce"])
    assert store.products()[1] == 2


@pytest.mark.parametrize("market", ["wb", "ozon"])
async def test_article_preview_requires_owner_confirmation(ui, store, market):
    await ui.message(message("/add"))
    await click(ui, "add:article")
    await click(ui, "market:" + market)
    await ui.message(message("123456"))
    assert store.draft(42)["step"] == "article_confirm"
    assert store.products()[1] == 0
    await click(ui, "article:yes")
    assert store.draft(42)["step"] == "name"


async def test_cancel_timeout_stale_button(ui, store):
    from paintwatch.database.models import Draft

    await ui.message(message("/add"))
    await click(ui, "add:link")
    await ui.message(message("/cancel"))
    assert store.draft(42) is None
    await click(ui, "batch:next")
    assert store.products()[1] == 0
    store.save_draft(42, {"step": "links"}, 60)
    with store.transaction() as session:
        session.get(Draft, 42).expires_at = 0
    await ui.message(message("https://ozon.ru/product/123/"))
    assert store.draft(42) is None
    assert "истекло" in ui.bot.send_message.call_args.args[1]


async def test_private_commands_work_through_dispatcher(ui):
    # Диспетчеризация aiogram: нет общего хендлера, перехватывающего команды.
    dispatcher = Dispatcher()
    ui.attach(dispatcher)
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    try:
        await dispatcher.feed_update(
            bot, Update(update_id=999, message=message("/tags"))
        )
        assert "порог" in ui.bot.send_message.call_args.args[1]
    finally:
        await bot.session.close()


async def test_diagnostics_actual_calls_and_no_fake_success():
    bot = AsyncMock()
    bot.get_me.return_value = SimpleNamespace(id=101)
    bot.get_chat.return_value = SimpleNamespace(
        id=-100123, type="supergroup", permissions=None
    )
    bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
    lines = await group_access(bot, -100123)
    assert any("подтверждены" in text for text in lines)
    bot.get_me.assert_awaited_once()
    bot.get_chat_member.assert_awaited_once_with(-100123, 101)
    bot.get_chat.side_effect = TelegramForbiddenError(
        GetChat(chat_id=-100123), "forbidden"
    )
    lines = await group_access(bot, -100123)
    assert any("TelegramForbiddenError" in text for text in lines)
    assert not any("Права на отправку: подтверждены" in text for text in lines)


async def test_group_test_sends_immediately_and_does_not_mutate_prices(
    ui, store, product
):
    before = store.history(product)
    store.set_group(-100123)
    ui.bot.get_chat.return_value = SimpleNamespace(id=-100123, type="supergroup")
    ui.bot.send_message.return_value = SimpleNamespace(
        chat=SimpleNamespace(id=-100123), message_id=5
    )
    await ui.message(message("📨 Проверить отправку в группу"))
    assert ui.bot.send_message.call_args_list[0].args[0] == -100123
    assert "тестовая" in ui.bot.send_message.call_args_list[0].args[1].lower()
    assert before == store.history(product)
    with store.sessions() as session:
        assert list(session.scalars(select(Notification))) == []


async def test_group_not_configured_does_not_send():
    bot = AsyncMock()
    result = await send_test_group(bot, None)
    assert "не настроена" in result
    bot.send_message.assert_not_called()


async def test_list_filters_pagination_and_history(ui, store, product):
    await ui.message(message("/list"))
    await click(ui, "filter:market:wb")
    assert '"wb"' in store.setting("list_filter")
    await click(ui, f"history:{product}")
    assert "Изменения достоверной цены" in ui.bot.send_message.call_args.args[1]
