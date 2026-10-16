import asyncio
import logging
import time

from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter, TelegramServerError)


class Sender:
    def __init__(self, store, bot):
        self.store, self.bot = store, bot
        self.lock = asyncio.Lock()

    async def deliver_one(self):
        async with self.lock:
            group = self.store.setting("group_id")
            if not group:
                return False
            q = self.store.claim_notification(int(time.time()))
            if not q:
                return False
            try:
                result = await self.bot.send_message(int(group), q.text, parse_mode="HTML", disable_web_page_preview=True)
            except TelegramRetryAfter as exc:
                self.store.finish_notification(q.id, "pending" if q.attempts < 8 else "failed",
                    "Telegram ограничил частоту; максимум 8 автоматических попыток", max(1, int(exc.retry_after)))
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                self.store.finish_notification(q.id, "failed", type(exc).__name__ + ": проверьте группу и права")
            except (TelegramNetworkError, TelegramServerError):
                self.store.finish_notification(q.id, "uncertain", "Сбой связи после начала отправки; доставка неизвестна")
            except TelegramAPIError as exc:
                self.store.finish_notification(q.id, "failed", type(exc).__name__)
            else:
                self.store.finish_notification(q.id, "sent", message_id=result.message_id)
            return True

    async def run(self):
        while True:
            try:
                await self.deliver_one()
            except Exception as exc:
                logging.getLogger(__name__).error("Queue failure: %s", type(exc).__name__)
            await asyncio.sleep(3)
