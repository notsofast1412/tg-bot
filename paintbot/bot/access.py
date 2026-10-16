import time
import secrets
from dataclasses import dataclass, field

from aiogram import BaseMiddleware


def authorized(user_id, chat_type, owner_id):
    return user_id == owner_id and chat_type == "private"


class OwnerOnly(BaseMiddleware):
    def __init__(self, owner_id, store):
        self.owner_id, self.store = owner_id, store

    async def __call__(self, handler, event, data):
        message = event.message or (event.callback_query.message if event.callback_query else None)
        user = event.message.from_user if event.message else (event.callback_query.from_user if event.callback_query else None)
        if not message or not user or not authorized(user.id, message.chat.type, self.owner_id):
            if event.callback_query:
                await event.callback_query.answer("Управление доступно владельцу в личном чате", show_alert=True)
            return
        if self.store.seen_update(event.update_id):
            if event.callback_query:
                await event.callback_query.answer("Уже обработано")
            return
        return await handler(event, data)


@dataclass
class Flow:
    step: str
    updated: float = field(default_factory=time.monotonic)
    data: dict = field(default_factory=dict)
    nonce: str = field(default_factory=lambda: secrets.token_hex(4))


class Flows:
    """Single owner, ephemeral drafts: a restart safely discards unconfirmed input."""
    def __init__(self, ttl):
        self.ttl, self.current = ttl, None

    def start(self, step, **data):
        self.current = Flow(step, data=data)
        return self.current

    def get(self):
        if self.current and time.monotonic() - self.current.updated > self.ttl:
            self.current = None
            raise ValueError("Время сценария истекло. Начните добавление заново")
        if self.current:
            self.current.updated = time.monotonic()
        return self.current

    def cancel(self):
        self.current = None
