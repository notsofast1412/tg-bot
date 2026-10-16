from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from paintbot.services.diagnostics import can_send, diagnose
from paintbot.scheduler.monitor import Monitor
from paintbot.services.notifications import Sender


def test_inflight_check_recovered_without_losing_threshold_state(store, card, product_id):
    store.record(product_id, replace(card, price=39900), None, 600)
    store.begin_attempt(product_id, 600)
    assert store.product(product_id).status == "Проверка выполняется"
    store.recover()
    assert "прервана" in store.product(product_id).status
    assert store.product(product_id).last_price == 39900
    store.record(product_id, replace(card, price=39000), None, 600)
    assert len(store.queue()) == 1


@pytest.mark.parametrize("status,permissions,expected", [
    ("administrator", False, True), ("creator", False, True),
    ("member", True, True), ("member", False, False), ("left", True, False), ("kicked", True, False)])
def test_group_permissions(status, permissions, expected):
    assert can_send(SimpleNamespace(permissions=SimpleNamespace(can_send_messages=permissions)),
                    SimpleNamespace(status=status)) == expected


async def test_diagnostics_does_not_report_success_on_group_error(store):
    bot = AsyncMock()
    store.setting("group_id", -100123)
    bot.get_chat.side_effect = TelegramForbiddenError(method=SendMessage(chat_id=-100123, text="x"), message="forbidden")
    result = await diagnose(bot, store, Monitor(store, {}))
    assert "проверка неуспешна" in result
    assert "право отправки: да" not in result


async def test_notification_retry_limit(store, card, product_id):
    store.setting("group_id", -100123)
    store.record(product_id, replace(card, price=39900), None, 600)
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramRetryAfter(method=SendMessage(chat_id=-100123,text="x"), message="retry", retry_after=1)
    sender = Sender(store, bot)
    for i in range(8):
        row = store.queue()[0]
        store.finish_notification(row.id, "pending")
        await sender.deliver_one()
    assert store.queue()[0].state == "failed"
    assert store.queue()[0].attempts == 8
    assert not await sender.deliver_one()
