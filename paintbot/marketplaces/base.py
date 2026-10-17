import asyncio
import json
import random
import ssl
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from paintbot.services.formatting import kopecks
from .links import CardRef, MarketError, parse_link


@dataclass
class Card:
    marketplace: str
    card_id: str
    url: str
    title: str
    article: str
    price: int | None = None
    attributes: dict = field(default_factory=dict)
    source: str = ""
    warning: str | None = None


class Adapter:
    market = ""

    def __init__(self, client=None, gap=5):
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(20), follow_redirects=False, trust_env=False, verify=ssl.create_default_context(),
            headers={"User-Agent": "PaintPriceMonitor/1.0 (personal price monitoring)", "Accept-Language": "ru-RU,ru;q=0.9"})
        self.gap = gap
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.blocked_until = 0

    async def request(self, url, *, allow_redirect=False):
        async with self.lock:
            if self.blocked_until > time.time():
                raise MarketError("Площадка запросила паузу после HTTP 429", retry_after=int(self.blocked_until - time.time()) + 1)
            for attempt in range(2):
                await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                # Vary request spacing without ever shortening the configured minimum.
                self.next_request = time.monotonic() + random.uniform(self.gap, self.gap * 1.5)
                try:
                    response = await self.client.get(url)
                except httpx.RequestError as exc:
                    if attempt == 0:
                        await asyncio.sleep(2)
                        continue
                    raise MarketError(f"Сетевая ошибка: {type(exc).__name__}") from None
                if response.status_code == 429:
                    retry = response.headers.get("Retry-After", "600")
                    try:
                        delay = float(retry)
                    except ValueError:
                        try:
                            delay = parsedate_to_datetime(retry).timestamp() - time.time()
                        except (ValueError, TypeError):
                            delay = 600
                    delay = max(600, min(delay, 86400))
                    self.blocked_until = time.time() + delay
                    raise MarketError("HTTP 429: площадка ограничила частоту; проверка отложена", retry_after=int(delay))
                if response.status_code in (401, 403):
                    raise MarketError(f"HTTP {response.status_code}: доступ ограничен; обход защиты не выполняется")
                if response.status_code >= 500 and attempt == 0:
                    await asyncio.sleep(2)
                    continue
                if response.is_redirect:
                    # The caller must validate the destination before another request.
                    if allow_redirect:
                        return response
                    raise MarketError("Перенаправление карточки: требуется прямая доступная ссылка")
                if response.status_code != 200:
                    raise MarketError(f"HTTP {response.status_code}: карточка не получена")
                if len(response.content) > 8_000_000:
                    raise MarketError("Ответ слишком большой")
                age = response.headers.get("age")
                try:
                    stale = age is not None and float(age) > 120
                except ValueError:
                    stale = True
                if stale:
                    raise MarketError("Ответ из кэша старше двух минут; не считается актуальной ценой")
                return response
        raise MarketError("Запрос не выполнен")

    async def fetch(self, ref: CardRef) -> Card:
        if ref.marketplace != self.market:
            raise MarketError("Неверный адаптер")
        url, visited = ref.url, set()
        for _ in range(4):
            if url in visited:
                raise MarketError("Цикл перенаправлений карточки")
            visited.add(url)
            response = await self.request(url, allow_redirect=True)
            if not response.is_redirect:
                return self.parse_html(ref, response.text)
            location = response.headers.get("location")
            if not location:
                raise MarketError("Перенаправление без адреса карточки")
            target = urljoin(url, location)
            if urlsplit(target).path.rstrip("/") == "/showcaptcha":
                raise MarketError("Площадка направила на CAPTCHA; получение данных остановлено, обход не выполняется")
            other = parse_link(target)  # rejects credentials, ports and unrelated hosts
            if urlsplit(target).scheme != "https" or (other.marketplace, other.card_id) != (ref.marketplace, ref.card_id):
                raise MarketError("Перенаправление на другой товар или небезопасный адрес")
            url = target
        raise MarketError("Слишком много перенаправлений карточки")

    def parse_html(self, ref, html):
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
        if any(word in title for word in ("captcha", "доступ ограничен", "проверка безопасности", "ой!")):
            raise MarketError("Страница защиты/проверки доступа; обход не выполняется")
        candidates = []

        def walk(node):
            if isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, dict):
                kind = node.get("@type")
                if kind == "Product" or (isinstance(kind, list) and "Product" in kind):
                    candidates.append(node)
                if "@graph" in node:
                    walk(node["@graph"])

        for script in soup.select('script[type="application/ld+json"]'):
            try:
                walk(json.loads(script.string or script.get_text()))
            except (ValueError, TypeError):
                continue
        exact = []
        for node in candidates:
            urls = [node.get("url"), node.get("@id")]
            identities = []
            for url in urls:
                if isinstance(url, str):
                    try:
                        identities.append(parse_link(url))
                    except MarketError:
                        pass
            # Canonical URL alone does not bind arbitrary recommendation JSON-LD to this card.
            base_id = ref.card_id.split("|")[0]
            article_id = base_id.removeprefix("offer:").removeprefix("card:")
            id_match = any(str(node.get(k, "")) == article_id for k in ("sku", "productID"))
            url_match = any(i.marketplace == ref.marketplace and i.card_id == ref.card_id for i in identities)
            if any(isinstance(u, str) and u for u in urls) and not url_match:
                continue
            if "|" in ref.card_id and not url_match:
                continue
            if not url_match and not id_match:
                continue
            if not node.get("name"):
                continue
            exact.append(node)
        if len(exact) != 1:
            raise MarketError("Не найдена однозначная Product JSON-LD с подтверждённым ID карточки")
        node = exact[0]
        attrs = {k: node[k] for k in ("color", "size", "material", "weight") if node.get(k)}
        # Preserve named attributes, without inferring packaging from a title or tag.
        for prop in node.get("additionalProperty", []) if isinstance(node.get("additionalProperty"), list) else []:
            if isinstance(prop, dict) and prop.get("name") and prop.get("value"):
                attrs[str(prop["name"])] = str(prop["value"])
        card = Card(ref.marketplace, ref.card_id, ref.url, str(node["name"])[:500],
                    str(node.get("sku") or article_id), attributes=attrs, source="HTML Product JSON-LD")
        offers = node.get("offers", [])
        offers = [offers] if isinstance(offers, dict) else offers
        if not isinstance(offers, list) or len(offers) != 1 or not isinstance(offers[0], dict):
            card.warning = "Несколько предложений или цена отсутствует; выбор продавца неоднозначен"
            return card
        offer = offers[0]
        if offer.get("@type") not in ("Offer", None) or "lowPrice" in offer or "highPrice" in offer:
            card.warning = "Агрегированная цена не является ценой конкретного предложения"
            return card
        if offer.get("priceCurrency") != "RUB":
            card.warning = "Нет подтверждённой цены в RUB"
            return card
        availability = str(offer.get("availability", "")).rstrip("/").rsplit("/", 1)[-1]
        if availability != "InStock":
            card.warning = "Доступность покупки не подтверждена"
            return card
        if offer.get("priceValidUntil"):
            from datetime import date
            try:
                if date.fromisoformat(offer["priceValidUntil"][:10]) < date.today():
                    card.warning = "Срок действия цены истёк"
                    return card
            except (ValueError, TypeError):
                card.warning = "Неизвестный срок действия цены"
                return card
        # If an offer points to a different card, its price must not be used.
        if offer.get("url"):
            try:
                other = parse_link(offer["url"])
                if other != ref:
                    raise MarketError("Предложение относится к другой карточке/продавцу")
            except MarketError:
                card.warning = "Не совпадает ссылка предложения"
                return card
        try:
            card.price = kopecks(offer["price"])
        except (ValueError, KeyError, TypeError):
            card.warning = "Нет достоверной положительной цены"
        return card
