import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from pydantic import ValidationError

from paintbot.bot.access import OwnerOnly
from paintbot.bot.handlers import make_router
from paintbot.bot.session import TelegramSession
from paintbot.config import Config
from paintbot.database.store import Store
from paintbot.marketplaces import make_adapters
from paintbot.scheduler.monitor import Monitor
from paintbot.services.notifications import Sender
from paintbot.services.runtime import instance_lock


async def run(config):
    store = Store(config.database_path)
    store.seed()  # Schema must be upgraded with Alembic first.
    store.recover()
    if config.group_id is not None and store.setting("group_id") is None:
        if config.group_id >= 0:
            raise ValueError("GROUP_ID должен быть отрицательным")
        store.setting("group_id", config.group_id)
    bot = Bot(config.bot_token.get_secret_value(), session=TelegramSession(), default=DefaultBotProperties(parse_mode="HTML"))
    adapters = make_adapters(config.request_gap_seconds)
    monitor = Monitor(store, adapters, config.check_interval_seconds)
    sender = Sender(store, bot)
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnly(config.owner_id, store))
    dispatcher.include_router(make_router(store, adapters, monitor, bot, config.scenario_ttl_seconds))
    tasks = []
    try:
        await bot.get_me()  # A real authentication request; do not start on an invalid token.
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError("У бота настроен webhook. Осознанно отключите его перед запуском long polling; существующая интеграция не изменена")
        tasks = [asyncio.create_task(monitor.run()), asyncio.create_task(sender.run())]
        await dispatcher.start_polling(bot, handle_as_tasks=False, allowed_updates=["message", "callback_query"],
                                       close_bot_session=False)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        for adapter in adapters.values():
            await adapter.client.aclose()
        store.engine.dispose()


def main():
    os.umask(0o077)
    try:
        config = Config()
    except ValidationError as exc:
        # Pydantic's default error rendering can include input secret values.
        names = ", ".join(".".join(map(str, item["loc"])) for item in exc.errors())
        raise SystemExit(f"Проверьте конфигурацию: {names}. См. .env.example") from None
    log_dir = config.database_path.resolve().parent
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(), RotatingFileHandler(log_dir / "paintbot.log",
                                  maxBytes=5_000_000, backupCount=5)])
    # HTTP library debug logs may expose token-bearing Telegram request URLs.
    for name in ("httpx", "httpcore", "aiohttp", "aiogram.event", "aiogram.dispatcher"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    with instance_lock(config.database_path):
        asyncio.run(run(config))


if __name__ == "__main__":
    main()
