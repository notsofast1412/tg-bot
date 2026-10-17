"""Readiness check and one real browser price request; no Telegram messages."""
import asyncio
import json

import httpx

from paintbot.marketplaces import WildberriesAdapter
from paintbot.marketplaces.links import MarketError, parse_link
from paintbot.marketplaces.wb_browser import ENDPOINT, KEY_PATH


async def main():
    try:
        key = KEY_PATH.read_text().strip()
    except OSError:
        raise SystemExit('First run scripts/setup_wb_browser.sh') from None
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        try:
            response = await client.get(ENDPOINT + '/health', headers={'Authorization': 'Bearer ' + key})
            connected = response.status_code == 200 and response.json().get('extension_connected') is True
        except (httpx.HTTPError, ValueError):
            raise SystemExit('Browser service unavailable; check paintbot-browser.service') from None
        print('Extension connected:', connected, flush=True)
        if not connected:
            raise SystemExit('Load the extension and keep DevTools open in the WB tab.')
    adapter = WildberriesAdapter(browser=True)
    try:
        ref = parse_link('https://www.wildberries.ru/catalog/518397236/detail.aspx?size=716573024')
        card = await adapter.fetch(ref)
        print(json.dumps({'card_id': card.card_id, 'name': card.title, 'price_kopecks': card.price,
                          'source': card.source, 'warning': card.warning}, ensure_ascii=False))
        if card.price is None:
            raise SystemExit(1)
    except MarketError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        await adapter.client.aclose()


if __name__ == '__main__':
    asyncio.run(main())
