import re
import time
import httpx

from .domain import display_time, rub


class TelegramError(Exception):
    def __init__(self, message, *, uncertain=False, delay=300):
        super().__init__(message)
        self.uncertain = uncertain
        self.delay = delay


class Telegram:
    def __init__(self, token, client=None):
        if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{25,}", token):
            raise ValueError("Токен бота имеет неверный формат")
        self.token = token
        self.client = client or httpx.AsyncClient(timeout=20)

    async def call(self, method, params=None):
        try:
            response = await self.client.post(
                f"https://api.telegram.org/bot{self.token}/{method}", json=params or {}
            )
            data = response.json()
            if not isinstance(data, dict) or (data.get("ok") and "result" not in data):
                raise ValueError("Invalid Telegram response")
        except (httpx.HTTPError, ValueError):
            raise TelegramError(
                "Ответ Telegram не получен. Для отправки проверьте группу перед повтором",
                uncertain=method == "sendMessage",
            ) from None
        if not data.get("ok"):
            code = data.get("error_code", response.status_code)
            delay = data.get("parameters", {}).get("retry_after", 300)
            raise TelegramError(
                f"Telegram отклонил запрос (код {code}). Проверьте токен, ID группы и права бота",
                delay=max(1, int(delay)),
            )
        return data["result"]

    async def diagnose(self, chat_id):
        me = await self.call("getMe")
        chat = await self.call("getChat", {"chat_id": chat_id})
        if chat.get("type") not in ("group", "supergroup"):
            raise TelegramError("Укажите ID группы, а не личного чата или канала")
        member = await self.call(
            "getChatMember", {"chat_id": chat_id, "user_id": me["id"]}
        )
        if (
            member.get("status") in ("left", "kicked")
            or member.get("can_send_messages") is False
        ):
            raise TelegramError("Бот не может писать в выбранную группу")
        return f"Связь проверена: @{me.get('username', 'бот')}, группа «{chat.get('title', '')}»"

    async def send(self, chat_id, text):
        return await self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:4000],
                "link_preview_options": {"is_disabled": True},
            },
        )

    async def close(self):
        await self.client.aclose()


def notification(event):
    market = (
        "Ozon — цена по карте"
        if event["market"] == "ozon"
        else "Wildberries — без WB Кошелька"
    )
    return f"🔻 Цена ниже порога!\n\n{event['name'][:250]}\n{market}\nЦена: {rub(event['current_price'])}\nПорог: {rub(event['threshold'])}\nТег: {event['tag']}\nПроверено: {display_time(event['last_success'])}\nСобытие №{event['id']}\n\n{event['url']}"


async def deliver(store, vault):
    chat_id = store.setting("chat_id")
    if not chat_id:
        return
    try:
        token = vault.read()
        if not token:
            return
        api = Telegram(token)
    except (ValueError, RuntimeError):
        return
    try:
        for event in reversed(store.queue()):
            if (
                event["state"] not in ("pending", "blocked")
                or event["next_try"] > time.time()
            ):
                continue
            if (
                not event["enabled"]
                or not event["below"]
                or event["generation"] != event["current_generation"]
                or event["threshold"] != event["current_threshold"]
            ):
                store.delivery(event["id"], "cancelled", "Условие больше не действует")
                continue
            if (
                not event["last_success"]
                or event["current_status"] != "Проверено"
                or time.time() - event["last_success"] > 900
                or event["last_attempt"] != event["last_success"]
            ):
                continue
            try:
                # Failure before sendMessage is safe to retry.
                await api.call("getMe")
            except TelegramError as error:
                store.delivery(event["id"], "pending", str(error), error.delay)
                break
            store.delivery(event["id"], "sending")
            try:
                await api.send(chat_id, notification(event))
            except TelegramError as error:
                store.delivery(
                    event["id"],
                    "uncertain" if error.uncertain else "blocked",
                    str(error),
                    error.delay,
                )
                break
            else:
                store.delivery(event["id"], "sent")
            # Stay well below Telegram's group message rate.
            import asyncio

            await asyncio.sleep(3)
    finally:
        await api.close()
