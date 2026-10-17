"""Local bridge to responses observed by a user-installed Chrome DevTools extension."""
from pathlib import Path
import re

import httpx

from .links import MarketError

ROOT = Path(__file__).resolve().parents[2]
KEY_PATH = ROOT / 'var/wb-browser.key'
ENDPOINT = 'http://127.0.0.1:8765'
SOURCE = 'https://www.wildberries.ru/__internal/u-card/cards/v4/detail'


def public_product(payload, nm):
    if not isinstance(payload, dict):
        raise MarketError('WB не подтвердил структуру товара')
    container = payload.get('data', payload)
    entries = container.get('products') if isinstance(container, dict) else None
    if not isinstance(entries, list):
        raise MarketError('WB не вернул список товаров')
    matches = [p for p in entries if isinstance(p, dict) and str(p.get('id')) == nm]
    if len(matches) != 1:
        raise MarketError('WB не подтвердил точный ID товара')
    product = matches[0]
    if not isinstance(product.get('name'), str) or not product['name'].strip():
        raise MarketError('WB не подтвердил название товара')
    sizes = product.get('sizes')
    if not isinstance(sizes, list) or len(sizes) > 100:
        raise MarketError('WB не подтвердил варианты товара')
    result = {'id': int(nm), 'name': product['name'][:500], 'sizes': []}
    if type(product.get('totalQuantity')) is int and product['totalQuantity'] >= 0:
        result['totalQuantity'] = product['totalQuantity']
    for size in sizes:
        if not isinstance(size, dict):
            raise MarketError('Некорректный вариант WB')
        if any(size.get(k) is not None and (type(size[k]) is not int or size[k] <= 0)
               for k in ('optionId', 'chrtId')):
            raise MarketError('Не подтверждён идентификатор варианта WB')
        entry = {k: size[k] for k in ('optionId', 'chrtId')
                 if type(size.get(k)) is int and size[k] > 0}
        price = size.get('price', {})
        value = price.get('product') if isinstance(price, dict) else None
        entry['price'] = {'product': value} if type(value) is int and 0 < value < 10**12 else {}
        if 'stocks' in size:
            stocks = size['stocks']
            if not isinstance(stocks, list) or any(
                    not isinstance(s, dict) or type(s.get('qty')) is not int or s['qty'] < 0
                    for s in stocks):
                raise MarketError('Не подтверждены остатки WB')
            entry['stocks'] = [{'qty': sum(s['qty'] for s in stocks)}]
        result['sizes'].append(entry)
    return result


async def fetch_browser(client, ref, key_path=None):
    path = key_path or KEY_PATH
    try:
        key = path.read_text().strip()
    except OSError:
        raise MarketError('Не настроен браузер WB: выполните scripts/setup_wb_browser.sh') from None
    if not re.fullmatch(r'[a-f0-9]{64}', key):
        raise MarketError('Некорректный локальный ключ браузера WB')
    try:
        response = await client.post(ENDPOINT + '/fetch', json={'url': ref.url},
                                     headers={'Authorization': 'Bearer ' + key}, timeout=85,
                                     follow_redirects=False)
    except httpx.HTTPError:
        raise MarketError('Служба браузера WB недоступна: проверьте paintbot-browser.service') from None
    if len(response.content) > 200_000:
        raise MarketError('Слишком большой ответ браузера WB')
    try:
        report = response.json()
    except (ValueError, UnicodeError):
        raise MarketError('Некорректный ответ службы браузера WB') from None
    if response.status_code != 200:
        code = report.get('error') if isinstance(report, dict) else None
        messages = {
            'extension_missing': 'Откройте DevTools (F12) в Chrome и загрузите расширение WB по инструкции',
            'busy': 'Браузер WB занят другой проверкой',
            'timeout': 'Chrome не подтвердил цену за 75 секунд; проверьте карточку через VNC',
            'page_refused': 'WB отказал в загрузке страницы в обычном Chrome',
            'api_refused': 'WB отказал в получении цены самой странице Chrome',
            'rate_limit': 'WB ограничил частоту; следующая проверка отложена',
            'invalid_result': 'Chrome вернул неподтверждённые данные WB',
        }
        raise MarketError(messages.get(code, 'Служба браузера WB не подтвердила цену'),
                          retry_after=600 if code == 'rate_limit' else 0)
    if (not isinstance(report, dict) or report.get('schema') != 1 or report.get('url') != ref.url
            or not isinstance(report.get('products'), list) or not 1 <= len(report['products']) <= 10):
        raise MarketError('Браузер не подтвердил нужную карточку WB')
    return report
