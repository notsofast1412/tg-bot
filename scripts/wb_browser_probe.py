"""Inspect one WB card through its public browser page; no direct API probes.

Uses the existing manual browser profile. Stop its other browser before running.
Only product fields, price blocks and bounded HTTP status metadata are reported;
no cookies, headers, full API responses, or account data are exported.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import parse_qs, urlsplit


DEFAULT_URL = 'https://www.wildberries.ru/catalog/518397236/detail.aspx?size=716573024'


def card_identity(url):
    try:
        parts = urlsplit(url)
        match = re.fullmatch(r'/catalog/([1-9]\d*)/detail\.aspx', parts.path)
        sizes = parse_qs(parts.query, keep_blank_values=True).get('size', [])
        if (parts.scheme != 'https' or parts.hostname not in ('www.wildberries.ru', 'wildberries.ru')
                or parts.username or parts.password or parts.port or not match
                or (sizes and (len(sizes) != 1 or not re.fullmatch(r'[1-9]\d*', sizes[0])))):
            raise ValueError()
        return match[1], sizes[0] if sizes else None
    except ValueError:
        raise ValueError('Use an HTTPS WB product URL with one optional numeric size') from None


def public_product(payload, nm_id):
    if not isinstance(payload, dict):
        return None
    container = payload.get('data', payload)
    if not isinstance(container, dict) or not isinstance(container.get('products'), list):
        return None
    matches = [p for p in container['products'] if isinstance(p, dict) and str(p.get('id')) == nm_id]
    if len(matches) != 1:
        return None
    product = matches[0]
    result = {'id': nm_id, 'name': str(product.get('name', ''))[:500], 'sizes': []}
    if type(product.get('totalQuantity')) is int:
        result['totalQuantity'] = product['totalQuantity']
    sizes = product.get('sizes', [])
    if not isinstance(sizes, list):
        return result
    for size in sizes[:100]:
        if not isinstance(size, dict):
            continue
        entry = {'chrtId': size.get('chrtId') if type(size.get('chrtId')) is int else None}
        if type(size.get('optionId')) is int:
            entry['optionId'] = size['optionId']
        price = size.get('price', {})
        entry['price'] = {k: v for k, v in price.items()
                          if k in ('basic', 'product', 'total', 'logistics', 'return') and type(v) is int} if isinstance(price, dict) else {}
        stocks = size.get('stocks', [])
        quantities = [stock['qty'] for stock in stocks if isinstance(stock, dict)
                      and type(stock.get('qty')) is int and stock['qty'] >= 0] if isinstance(stocks, list) else []
        if isinstance(stocks, list) and 'stocks' in size:
            entry['stock_qty'] = sum(quantities)
        result['sizes'].append(entry)
    return result


async def probe(url, browser=None):
    from playwright.async_api import async_playwright
    nm_id, selected_size = card_identity(url)
    root = Path('/opt/paintbot')
    executable = shutil.which(browser) if browser else (
        shutil.which('google-chrome-stable') or shutil.which('chromium'))
    managed = root / 'var/chromium/chromium-1187/chrome-linux/chrome'
    if not executable and not browser and managed.is_file():
        executable = str(managed)
    if not executable:
        raise RuntimeError('Requested browser is not installed')
    version = subprocess.run([executable, '--version'], check=True, capture_output=True,
                             text=True, timeout=10).stdout.strip()
    profile = root / ('var/manual-browser-profile' if Path(executable) == managed
                      else 'var/manual-browser-stable-profile')
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = {'browser': version, 'card_id': nm_id, 'size': selected_size,
              'responses': [], 'products': [], 'price_blocks': {}}
    pending = set()
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile), executable_path=executable, headless=False,
            chromium_sandbox=True, args=['--no-proxy-server'], locale='ru-RU')
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            async def inspect_response(response):
                try:
                    parts = urlsplit(response.url)
                    host = parts.hostname or ''
                    if not (host == 'wb.ru' or host.endswith('.wb.ru')
                            or host == 'wildberries.ru' or host.endswith('.wildberries.ru')):
                        return
                    if 'cards/' not in parts.path or '/detail' not in parts.path:
                        return
                    if len(output['responses']) < 30:
                        output['responses'].append({'host': host, 'path': parts.path, 'status': response.status})
                    if response.status != 200 or len(output['products']) >= 10:
                        return
                    product = public_product(await response.json(), nm_id)
                    if product and product not in output['products']:
                        output['products'].append(product)
                except Exception:
                    # Do not print URLs or response bodies from unexpected failures.
                    return

            def on_response(response):
                if len(pending) < 20:
                    task = asyncio.create_task(inspect_response(response))
                    pending.add(task)
                    task.add_done_callback(pending.discard)

            page.on('response', on_response)
            response = await page.goto(url, wait_until='domcontentloaded', timeout=45000)
            output['page_http'] = response.status if response else None
            # A denied document is not retried with different routes or identities.
            if response and response.status in (401, 403, 429):
                output['error'] = 'Browser page denied access'
            else:
                try:
                    await page.locator('h1').wait_for(timeout=20000)
                except Exception:
                    pass
                await page.wait_for_timeout(5000)
                try:
                    final_id, final_size = card_identity(page.url)
                except ValueError:
                    final_id, final_size = None, None
                output['same_card_and_size'] = (final_id, final_size) == (nm_id, selected_size)
                output['title'] = (await page.title())[:300]
                output['heading'] = (await page.locator('h1').all_text_contents())[:3]
                for selector in ('.price-block__final-price', '.price-block__wallet-price',
                                 '.price-block__old-price', '.price-block__price', '.product-page__price-block'):
                    texts = await page.locator(selector).all_text_contents()
                    output['price_blocks'][selector] = [text.strip()[:1200] for text in texts[:5]]
        finally:
            if pending:
                done, unfinished = await asyncio.wait(list(pending), timeout=5)
                for task in unfinished:
                    task.cancel()
                await asyncio.gather(*unfinished, return_exceptions=True)
            await context.close()
    # This diagnostic never submits prices to monitoring or sends messages.
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return int(bool(output.get('error')) or not output.get('same_card_and_size'))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', nargs='?', default=DEFAULT_URL)
    parser.add_argument('--browser')
    args = parser.parse_args()
    try:
        card_identity(args.url)
        raise SystemExit(asyncio.run(probe(args.url, args.browser)))
    except Exception as exc:
        print('Проверка не выполнена:', type(exc).__name__)
        print('Остановите прежний VNC-браузер через Ctrl+C; проверьте установку Playwright и браузера.')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
