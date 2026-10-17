from .base import Adapter, Card
from urllib.parse import urljoin, urlsplit

from .links import MarketError, is_ozon_short, parse_link


class WildberriesAdapter(Adapter):
    market = "wb"

    async def fetch(self, ref):
        if ref.marketplace != self.market:
            raise MarketError("Неверный адаптер")
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
        if selected:
            sizes = [size for size in sizes if str(size.get("chrtId")) == selected]
            attrs["Запрошенный вариант chrtId"] = selected
            if len(sizes) != 1:
                return Card("wb", ref.card_id, ref.url, product["name"][:500], nm_id, None, attrs,
                            "WB cards/v2", "Выбранный size не подтверждён единственным chrtId; цена другого варианта не используется")
            attrs["Вариант WB"] = {key: sizes[0][key] for key in ("name", "origName") if sizes[0].get(key)}
        prices = set()
        for size in sizes:
            # Only an available size with a direct product price qualifies.
            if sum(s.get("qty", 0) for s in size.get("stocks", [])) <= 0:
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


def validate_marketplace_proxy(value, variable_name):
    if value is None or not value.strip():
        return None
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not parsed.port:
            raise ValueError()
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError()
    except ValueError:
        # Exception text and chained errors must never reveal proxy credentials.
        raise MarketError(f"Некорректный {variable_name}: нужен HTTP(S)-прокси с хостом и портом") from None
    return value


def validate_wb_proxy(value):
    return validate_marketplace_proxy(value, "WB_PROXY_URL")


def make_adapters(gap=5, wb_proxy_url=None, ozon_proxy_url=None):
    wb_proxy = validate_wb_proxy(wb_proxy_url)
    ozon_proxy = validate_marketplace_proxy(ozon_proxy_url, "OZON_PROXY_URL")
    try:
        wb = WildberriesAdapter(gap=gap, proxy=wb_proxy)
        ozon = OzonAdapter(gap=gap, proxy=ozon_proxy)
    except (ValueError, TypeError):
        raise MarketError("Не удалось настроить прокси; проверьте WB_PROXY_URL/OZON_PROXY_URL без вывода реквизитов") from None
    return {"wb": wb, "ozon": ozon}
