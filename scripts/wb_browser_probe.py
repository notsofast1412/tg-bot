"""Inspect one WB card through its public browser page; no direct API probes.

Uses the existing manual browser profile. Stop its other browser before running.
Only product fields, price blocks and bounded HTTP status metadata are reported;
no cookies, headers, full API responses, or account data are exported.
"""
import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from email.utils import parsedate_to_datetime
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
    if not isinstance(product.get('name'), str) or not product['name'].strip():
        return None
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


def project_root():
    root = Path(__file__).resolve().parents[1]
    return root if (root / 'paintbot').is_dir() else Path('/opt/paintbot')


def retry_seconds(value):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            delay = 600
    try:
        return max(600, min(int(delay), 86400))
    except (ValueError, OverflowError):
        return 600


class BrowserError(Exception):
    pass


@contextmanager
def browser_lock(root):
    path = root / 'var/wb-browser.lock'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BrowserError('busy') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def probe(url, browser=None):
    from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
    nm_id, selected_size = card_identity(url)
    root = project_root()
    executable = shutil.which(browser) if browser else (
        shutil.which('google-chrome-stable') or shutil.which('chromium'))
    managed = root / 'var/chromium/chromium-1187/chrome-linux/chrome'
    if not executable and not browser and managed.is_file():
        executable = str(managed)
    if not executable:
        raise BrowserError('browser_missing')
    version = subprocess.run([executable, '--version'], check=True, capture_output=True,
                             text=True, timeout=10).stdout.strip()
    profile = root / ('var/manual-browser-profile' if Path(executable) == managed
                      else 'var/manual-browser-stable-profile')
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = {'schema': 1, 'browser': version, 'card_id': nm_id, 'size': selected_size,
              'responses': [], 'products': [], 'price_blocks': {}}
    pending = set()
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile), executable_path=executable, headless=False,
            chromium_sandbox=True, args=['--no-proxy-server'], locale='ru-RU')
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            cdp = await context.new_cdp_session(page)
            await cdp.send('Network.enable')
            await cdp.send('Network.setCacheDisabled', {'cacheDisabled': True})

            async def inspect_response(response):
                status = None
                try:
                    parts = urlsplit(response.url)
                    host = parts.hostname or ''
                    if (parts.scheme != 'https' or not (host == 'wb.ru' or host.endswith('.wb.ru')
                            or host == 'wildberries.ru' or host.endswith('.wildberries.ru'))
                            or 'cards/' not in parts.path or '/detail' not in parts.path):
                        return
                    allowed = (host in ('wildberries.ru', 'www.wildberries.ru')
                               and parts.path == '/__internal/u-card/cards/v4/detail')
                    status = {'host': host, 'path': parts.path, 'status': response.status,
                              'source_allowed': allowed}
                    if len(output['responses']) < 30:
                        output['responses'].append(status)
                    if not allowed:
                        status['skip_reason'] = 'different_source'
                        return
                    if response.status == 429:
                        output['retry_after'] = retry_seconds(response.headers.get('retry-after'))
                    if response.status != 200 or len(output['products']) >= 10:
                        return
                    age = float(response.headers.get('age', '0'))
                    if not math.isfinite(age) or age < 0:
                        status['skip_reason'] = 'invalid_cache_age'
                        return
                    status['age_seconds'] = age
                    if age > 120:
                        status['skip_reason'] = 'stale_cache'
                        return
                    payload = await response.json()
                    container = payload.get('data', payload) if isinstance(payload, dict) else None
                    entries = container.get('products') if isinstance(container, dict) else None
                    status['products_list'] = isinstance(entries, list)
                    if isinstance(entries, list):
                        status['product_count'] = len(entries)
                        status['matched_requested_id_count'] = sum(
                            isinstance(entry, dict) and str(entry.get('id')) == nm_id for entry in entries)
                    product = public_product(payload, nm_id)
                    status['matched_public_product'] = bool(product)
                    if product and product not in output['products']:
                        output['products'].append(product)
                except Exception as exc:
                    # Never export cookies, headers or raw responses.
                    if status is not None:
                        status['read_error'] = type(exc).__name__
                    return

            def on_response(response):
                if len(pending) < 20:
                    task = asyncio.create_task(inspect_response(response))
                    pending.add(task)
                    task.add_done_callback(pending.discard)

            page.on('response', on_response)
            try:
                response = await page.goto(url, wait_until='domcontentloaded', timeout=45000)
                output['page_http'] = response.status if response else None
                if response and response.status in (401, 403, 429):
                    output['error'] = f'http_{response.status}'
                    if response.status == 429:
                        output['retry_after'] = retry_seconds(response.headers.get('retry-after'))
                else:
                    # h1 can be a rating widget; wait for the actual product JSON instead.
                    for _ in range(40):
                        if output['products']:
                            await page.wait_for_timeout(1000)
                            break
                        if any(item['source_allowed'] and item['status'] in (401, 403, 429)
                               for item in output['responses']):
                            break
                        await page.wait_for_timeout(500)
                    try:
                        final_id, final_size = card_identity(page.url)
                    except ValueError:
                        final_id, final_size = None, None
                    output['same_card_and_size'] = (final_id, final_size) == (nm_id, selected_size)
                    output['title'] = (await page.title())[:300]
                    output['heading'] = (await page.locator('h1').all_text_contents())[:3]
                    if not output['same_card_and_size']:
                        output['error'] = 'different_card'
                    elif re.search(r'captcha|antibot|доступ ограничен', output['title'], re.I):
                        output['error'] = 'challenge'
                    for selector in ('.price-block__final-price', '.price-block__wallet-price',
                                     '.price-block__old-price', '.price-block__price', '.product-page__price-block'):
                        texts = await page.locator(selector).all_text_contents()
                        output['price_blocks'][selector] = [text.strip()[:1200] for text in texts[:5]]
            except BrowserTimeout:
                output['error'] = 'timeout'
        finally:
            if pending:
                _, unfinished = await asyncio.wait(list(pending), timeout=5)
                for task in unfinished:
                    task.cancel()
                await asyncio.gather(*unfinished, return_exceptions=True)
            await context.close()
    if not output.get('error') and not output['products']:
        denied = next((item['status'] for item in output['responses']
                       if item['source_allowed'] and item['status'] in (401, 403, 429)), None)
        output['error'] = f'http_{denied}' if denied else 'no_product'
    return output


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', nargs='?', default=DEFAULT_URL)
    parser.add_argument('--browser')
    parser.add_argument('--machine', action='store_true', help='Compact JSON for the bot worker bridge')
    args = parser.parse_args()
    try:
        card_identity(args.url)
        with browser_lock(project_root()):
            output = asyncio.run(probe(args.url, args.browser))
    except BrowserError as exc:
        output = {'schema': 1, 'error': str(exc)}
    except ImportError:
        output = {'schema': 1, 'error': 'browser_missing'}
    except Exception:
        output = {'schema': 1, 'error': 'browser_failed'}
    print(json.dumps(output, ensure_ascii=False, indent=None if args.machine else 2))
    raise SystemExit(int(bool(output.get('error'))))


if __name__ == '__main__':
    main()
