"""Read-only live checks; optional explicit group test-send."""
import argparse
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from pydantic import ValidationError

from paintbot.bot.session import ProxyAwareSession
from paintbot.config import Config
from paintbot.database.store import Store
from paintbot.services.diagnostics import group_access
from scripts.manage import probe

URLS = [
    "https://ozon.ru/t/8M08OsF",
    "https://www.wildberries.ru/catalog/29220652/detail.aspx?size=65383223",
]


async def verify(config, send_test=False):
    failures = 0
    print("Маркетплейсы: реальные запросы по двум ссылкам владельца")
    failures |= await probe(URLS,
        wb_proxy_url=config.wb_proxy_url.get_secret_value() if config.wb_proxy_url else None,
        ozon_proxy_url=config.ozon_proxy_url.get_secret_value() if config.ozon_proxy_url else None)
    store = Store(config.database_path)
    try:
        group = store.setting("group_id") or config.group_id
    finally:
        store.engine.dispose()
    bot = Bot(config.bot_token.get_secret_value(), session=ProxyAwareSession())
    try:
        me = await bot.get_me()
        print(f"Telegram getMe: успешно, bot ID {me.id}")
        info = await bot.get_webhook_info()
        if info.url:
            failures = 1
            print("Webhook настроен: long polling не запустится, существующая интеграция не изменена")
        else:
            print("Webhook отсутствует: long polling разрешён")
        if group:
            allowed, status = await group_access(bot, int(group))
            print(f"Группа: статус {status}; право отправки: {'да' if allowed else 'нет'}")
            if not allowed:
                failures = 1
            if send_test:
                await bot.send_message(int(group), "✅ Проверка Paint Monitor с сервера. Это тест, а не уведомление о цене.")
                print("Тестовая отправка: подтверждена Telegram")
        else:
            print("Группа пока не настроена; после запуска используйте /setgroup в личном чате")
            if send_test:
                failures = 1
                print("Тестовая отправка не выполнялась: группа отсутствует")
    except (TelegramAPIError, ValueError) as exc:
        failures = 1
        print("Проверка Telegram неуспешна: " + type(exc).__name__)
    finally:
        await bot.session.close()
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-test", action="store_true", help="Фактически отправить тест в настроенную группу")
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        config = Config()
    except ValidationError:
        raise SystemExit("Сначала выполните python -m scripts.configure; значения конфигурации не выводятся") from None
    raise SystemExit(asyncio.run(verify(config, args.send_test)))


if __name__ == "__main__":
    main()
