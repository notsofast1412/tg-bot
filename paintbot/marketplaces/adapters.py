from .base import Adapter, Card
import asyncio
import time
from urllib.parse import urljoin, urlsplit

from .links import MarketError, is_ozon_short, parse_link
from . import wb_browser


class WildberriesAdapter(Adapter):
    market = "wb"

    def __init__(self, client=None, gap=5, browser=None):
        super().__init__(client=client, gap=gap)
        self.use_browser = wb_browser.KEY_PATH.is_file() if browser is None else browser

    async def fetch(self, ref):
        if ref.marketplace != self.market:
            raise MarketError("Неверный адаптер")
        if self.use_browser:
            if parse_link(ref.url) != ref:
                raise MarketError('Неверная карточка WB')
            async with self.lock:
                if self.blocked_until > time.time():
                    raise MarketError('WB запросил паузу после HTTP 429',
                                      retry_after=int(self.blocked_until - time.time()) + 1)
                await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + self.gap
                try:
                    report = await wb_browser.fetch_browser(self.client, ref)
                    nm = ref.card_id.split('|')[0]
                    cards = [self.parse_api(ref, {'products': [wb_browser.public_product(
                        {'products': [product]}, nm)]}) for product in report['products']]
                    card = cards[-1]
                    if len({c.price for c in cards}) != 1:
                        card.price = None
                        card.warning = 'Ответы WB содержат разные цены или остатки; цена не подтверждена'
                    card.source = 'WB Chrome DevTools; price.product без кошелька; регион браузера'
                    if report.get('dest'):
                        card.attributes['WB dest'] = report['dest']
                    return card
                except MarketError as exc:
                    if exc.retry_after:
                        self.blocked_until = time.time() + exc.retry_after
                    raise
        try:
            nm_id = ref.card_id.split("|", 1)[0]
            response = await self.request("https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&nm=" + nm_id)
            return self.parse_api(ref, response.json())
        except (MarketError, ValueError, KeyError, TypeError) as exc:
            # A block is never bypassed with another technique.
            if isinstance(exc, MarketError) and any(w in str(exc) for w in ("403", "401", "429")):
                raise
            try:
                card = await super().fetch(ref)
                card.source += " (резерв WB)"
                return card
            except MarketError as fallback:
                raise MarketError(f"WB API: {str(exc)[:180]}; HTML: {fallback}") from None

    def parse_api(self, ref, payload):
        nm_id, _, selected = ref.card_id.partition("|size:")
        products = payload.get("data", {}).get("products", payload.get("products", []))
        matches = [p for p in products if str(p.get("id")) == nm_id]
        if len(matches) != 1 or not matches[0].get("name"):
            raise MarketError("WB API не подтвердил точный ID товара")
        product = matches[0]
        attrs = {k: product[k] for k in ("brand", "colors", "supplier") if product.get(k)}
        sizes = product.get("sizes", [])
        single_variant = len(sizes) == 1

        def variant_id(size):
            old, new = size.get('chrtId'), size.get('optionId')
            if old is not None and new is not None and str(old) != str(new):
                return None
            value = old if old is not None else new
            return str(value) if type(value) in (int, str) else None

        if selected:
            sizes = [size for size in sizes if variant_id(size) == selected]
            attrs["Запрошенный вариант chrtId"] = selected
            if len(sizes) != 1:
                return Card("wb", ref.card_id, ref.url, product["name"][:500], nm_id, None, attrs,
                            "WB cards", "Выбранный size не подтверждён единственным chrtId/optionId; цена другого варианта не используется")
            attrs["Вариант WB"] = {key: sizes[0][key] for key in ("name", "origName") if sizes[0].get(key)}
        prices = set()
        for size in sizes:
            # Only an available size with a direct product price qualifies.
            available = (sum(s.get('qty', 0) for s in size['stocks']) > 0 if 'stocks' in size else
                         single_variant and type(product.get('totalQuantity')) is int and product['totalQuantity'] > 0)
            if not available:
                continue
            value = size.get("price", {}).get("product")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                prices.add(value)
        price = next(iter(prices)) if len(prices) == 1 else None
        warning = None if price is not None else "Нет доступной цены или цены вариантов различаются"
        return Card("wb", ref.card_id, ref.url, product["name"][:500], nm_id, price, attrs,
                    "WB cards/v2; обычная цена без кошелька; регион dest=-1257786", warning)


class OzonAdapter(Adapter):
    market = "ozon"

    async def resolve_short(self, url):
        if not is_ozon_short(url):
            raise MarketError("Некорректная короткая ссылка Ozon")
        # Only the public Ozon share service, never arbitrary URL shorteners.
        url = "https://" + url.split("://", 1)[1]
        visited = set()
        for _ in range(3):
            if url in visited:
                raise MarketError("Цикл коротких ссылок Ozon")
            visited.add(url)
            response = await self.request(url, allow_redirect=True)
            location = response.headers.get("location") if response.is_redirect else None
            if not location:
                raise MarketError("Короткая ссылка Ozon не раскрылась в карточку")
            target = urljoin(url, location)
            if urlsplit(target).scheme != "https":
                raise MarketError("Небезопасное перенаправление короткой ссылки")
            if is_ozon_short(target):
                url = target
                continue
            ref = parse_link(target)
            if ref.marketplace != "ozon":
                raise MarketError("Короткая ссылка ведёт на другую площадку")
            return ref
        raise MarketError("Слишком много перенаправлений короткой ссылки")


def make_adapters(gap=5):
    return {"wb": WildberriesAdapter(gap=gap), "ozon": OzonAdapter(gap=gap)}
