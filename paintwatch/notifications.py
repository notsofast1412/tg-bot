import asyncio
from datetime import datetime, timezone
from html import escape

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from .domain import MARKET_NAMES, rub


def notification_text(n):
    p = n["payload"]
    text = (
        f"🔻 <b>Цена ниже порога!</b>\n\n🎨 {escape(p['name'][:200])}\n{escape(p['card_name'][:300])}\n"
        f"🏪 {MARKET_NAMES[p['market']]}\n🔢 Артикул: {escape(p['article'])}\n"
        f"💰 Текущая цена: {rub(p['price'])}\n🎯 Порог: строго меньше {rub(p['threshold'])}\n"
    )
    if p["previous"] is not None:
        delta = p["price"] - p["previous"]
        text += f"📉 Изменение: {'−' if delta < 0 else '+'}{rub(abs(delta))}\n"
    observed = datetime.fromtimestamp(p["observed_at"], timezone.utc).strftime(
        "%d.%m.%Y %H:%M UTC"
    )
    return text + (
        f"🏷 {escape(p['tag'])}\n{escape(p['price_kind'])}\nПроверено: {observed}\n"
        f'<a href="{escape(p["url"], quote=True)}">🔗 Открыть товар</a>\nСобытие #{n["id"]}'
    )


class Sender:
    def __init__(self, store, bot):
        self.store, self.bot = store, bot
        self.lock = asyncio.Lock()

    async def send_one(self):
        async with self.lock:
            n = self.store.claim_notification()
            if not n:
                return False
            try:
                # При недоступном API сообщение ещё не отправлялось: безопасен автоповтор.
                await self.bot.get_me()
            except TelegramRetryAfter as e:
                self.store.finish_notification(
                    n["id"],
                    "pending",
                    "Telegram ограничил частоту",
                    delay=e.retry_after + 1,
                )
                return True
            except (TelegramAPIError, OSError, asyncio.TimeoutError):
                self.store.finish_notification(
                    n["id"],
                    "pending",
                    "Telegram недоступен до отправки",
                    delay=min(3600, 30 * 2 ** min(n["attempts"], 6)),
                )
                return True
            try:
                message = await self.bot.send_message(
                    n["chat_id"],
                    notification_text(n),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except TelegramRetryAfter as e:
                self.store.finish_notification(
                    n["id"],
                    "pending",
                    "Telegram ограничил частоту",
                    delay=e.retry_after + 1,
                )
            except (TelegramBadRequest, TelegramForbiddenError):
                self.store.finish_notification(
                    n["id"],
                    "blocked",
                    "Проверьте ID группы, участие и права бота",
                    delay=3600,
                )
            except (
                TelegramNetworkError,
                TelegramServerError,
                TelegramAPIError,
                OSError,
                asyncio.TimeoutError,
            ):
                # Bot API не имеет idempotency key. При потере ответа доставка неизвестна.
                # Автоповтор здесь может создать дубликат, поэтому требуется решение владельца.
                self.store.finish_notification(
                    n["id"],
                    "uncertain",
                    "Ответ Telegram потерян: сначала проверьте группу, затем /retry ID при необходимости",
                )
            else:
                self.store.finish_notification(
                    n["id"], "sent", message_id=message.message_id
                )
            return True

    async def run(self):
        while True:
            await self.send_one()
            await asyncio.sleep(2)
