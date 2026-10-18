from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from sqlalchemy import select

from paintwatch.database.models import Notification
from paintwatch.notifications import Sender, notification_text


def event(store, product, snapshot):
    store.apply(product, replace(snapshot, price=38900), None, 600)
    store.set_group(-100123)


async def test_actual_sender_invocation_and_no_duplicate(store, product, snapshot):
    event(store, product, snapshot)
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=77)
    sender = Sender(store, bot)
    assert await sender.send_one()
    assert not await sender.send_one()
    bot.send_message.assert_awaited_once()
    args, kwargs = bot.send_message.call_args
    assert args[0] == -100123 and "389.00 ₽" in args[1] and "#arton" in args[1]
    assert kwargs["parse_mode"] == "HTML"
    with store.sessions() as session:
        n = session.scalar(select(Notification))
        assert n.status == "sent" and n.message_id == 77


async def test_no_group_keeps_pending(store, product, snapshot):
    store.apply(product, replace(snapshot, price=38900), None, 600)
    bot = AsyncMock()
    assert not await Sender(store, bot).send_one()
    bot.send_message.assert_not_called()


async def test_definite_rejection_retried_after_config_fix(store, product, snapshot):
    event(store, product, snapshot)
    bot = AsyncMock()
    method = SendMessage(chat_id=-100123, text="test")
    bot.send_message.side_effect = TelegramBadRequest(method, "chat not found")
    sender = Sender(store, bot)
    assert await sender.send_one()
    with store.sessions() as session:
        assert session.scalar(select(Notification)).status == "blocked"
    store.set_group(-100456)
    bot.send_message.side_effect = None
    bot.send_message.return_value = SimpleNamespace(message_id=78)
    assert await sender.send_one()
    assert bot.send_message.call_args.args[0] == -100456


async def test_retry_after_durable_queue(store, product, snapshot):
    event(store, product, snapshot)
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramRetryAfter(
        SendMessage(chat_id=-100123, text="test"), "rate limit", 5
    )
    sender = Sender(store, bot)
    await sender.send_one()
    assert not await sender.send_one()
    with store.sessions() as session:
        n = session.scalar(select(Notification))
        assert n.status == "pending" and n.attempts == 1


async def test_uncertain_delivery_no_automatic_duplicate(store, product, snapshot):
    event(store, product, snapshot)
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramNetworkError(
        SendMessage(chat_id=-100123, text="test"), "timeout"
    )
    sender = Sender(store, bot)
    await sender.send_one()
    assert not await sender.send_one()
    uncertain = store.uncertain_notifications()
    assert len(uncertain) == 1 and bot.send_message.await_count == 1
    assert store.retry_uncertain(uncertain[0]["id"])


async def test_api_unavailable_before_send_retries_automatically(
    store, product, snapshot
):
    from aiogram.methods import GetMe

    event(store, product, snapshot)
    bot = AsyncMock()
    bot.get_me.side_effect = TelegramNetworkError(GetMe(), "Connection unavailable")
    await Sender(store, bot).send_one()
    bot.send_message.assert_not_called()
    with store.sessions() as session:
        notification = session.scalar(select(Notification))
        assert notification.status == "pending" and notification.next_attempt > 0


async def test_outdated_alert_not_sent_after_price_recovered(store, product, snapshot):
    event(store, product, snapshot)
    store.apply(product, replace(snapshot, price=42000), None, 600)
    bot = AsyncMock()
    assert not await Sender(store, bot).send_one()
    bot.send_message.assert_not_called()
    with store.sessions() as session:
        assert session.scalar(select(Notification)).status == "cancelled"


async def test_queue_waits_for_fresh_price_after_error(store, product, snapshot):
    from paintwatch.domain import FetchError

    event(store, product, snapshot)
    store.apply(product, None, FetchError("network", "нет сети"), 600)
    bot = AsyncMock()
    assert not await Sender(store, bot).send_one()
    bot.send_message.assert_not_called()
    assert store.statistics()["queue"] == 1


def test_first_event_does_not_invent_delta_and_html_escaped(store, snapshot):
    store.add_batch([replace(snapshot, price=38900)], "<краска>", 1, None, {"wb": 600})
    store.set_group(-100123)
    n = store.claim_notification()
    text = notification_text(n)
    assert "Изменение" not in text
    assert "&lt;краска&gt;" in text
