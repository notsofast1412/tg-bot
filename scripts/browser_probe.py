"""One public Ozon page in ordinary Chromium; no saved cookies or challenge solving.

Run using an isolated environment with playwright installed, from /opt/paintbot.
"""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout


async def main():
    os.umask(0o077)
    metadata = json.loads(Path('/etc/paintbot-ru-proxy.json').read_text())
    proxy = 'http://' + metadata['proxy'] + ':3128'
    output = Path('var/browser-probe')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    failed_hosts = set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, chromium_sandbox=True, proxy={'server': proxy})
        try:
            context = await browser.new_context(locale='ru-RU')
            page = await context.new_page()
            page.on('requestfailed', lambda request: failed_hosts.add(
                urlsplit(request.url).hostname or 'unknown'))
            try:
                response = await page.goto(
                    'https://www.ozon.ru/product/196525011/',
                    wait_until='domcontentloaded', timeout=45000)
                print('Первый HTTP-ответ:', response.status if response else 'нет')
                try:
                    await page.wait_for_function(
                        "() => /в корзину|captcha|капч|подтвердите|доступ ограничен/i.test(document.body?.innerText || '')",
                        timeout=20000)
                except BrowserTimeout:
                    print('Ожидание карточки/проверки завершено по таймауту')
                print('Заголовок:', await page.title())
                print('Текст страницы (это диагностика, не подтверждённая цена):')
                print((await page.locator('body').inner_text(timeout=5000))[:1600])
                await page.screenshot(path=str(output / 'page.png'))
                print('Скриншот сохранён локально: var/browser-probe/page.png')
            finally:
                print('Хосты с неудачными запросами:', ', '.join(sorted(failed_hosts)[:20]) or 'нет')
        finally:
            await browser.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        # Browser failures may contain process logs, but this script never loads secrets.
        print(type(exc).__name__ + ': ' + str(exc)[:2500])
        raise SystemExit(1)
