import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from .bot.ui import UI
from .config import Config
from .database.store import Store
from .marketplaces import adapters
from .marketplaces.web import Web
from .notifications import Sender
from .runtime import configure_logging, instance_lock
from .services import Monitor


async def run(config):
    config.require_telegram()
    configure_logging(config)
    store = Store(config.database_url)
    store.seed()
    store.recover_queue()
    if config.notification_chat_id and not store.setting("group_id"):
        if config.notification_chat_id >= 0:
            raise ValueError(
                "NOTIFICATION_CHAT_ID должен быть ID группы (отрицательное число)"
            )
        store.set_group(config.notification_chat_id)
    session = AiohttpSession(proxy=os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"))
    bot = Bot(config.bot_token.get_secret_value(), session=session)
    dispatcher = Dispatcher()
    web = Web(config, store=store)
    market_adapters = adapters(web, config)
    monitor = Monitor(store, market_adapters, config)
    UI(store, market_adapters, config, monitor, bot).attach(dispatcher)
    sender = Sender(store, bot)

    async def polling():
        # Сохраняем pending updates. Работающий webhook не удаляется молча.
        info = await bot.get_webhook_info()
        if info.url:
            raise RuntimeError(
                "Настроен webhook; отключите его явно перед переходом на polling"
            )
        await dispatcher.start_polling(
            bot, allowed_updates=["message", "callback_query"], handle_as_tasks=False
        )

    tasks = [
        asyncio.create_task(monitor.run(), name="monitor"),
        asyncio.create_task(sender.run(), name="sender"),
        asyncio.create_task(polling(), name="polling"),
    ]
    try:
        # Любая неожиданная остановка компонента завершает процесс для systemd restart.
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await web.close()
        await bot.session.close()
        store.engine.dispose()


def main():
    os.umask(0o077)
    config = Config()
    config.require_telegram()
    with instance_lock(config.data_dir):
        asyncio.run(run(config))


if __name__ == "__main__":
    main()
