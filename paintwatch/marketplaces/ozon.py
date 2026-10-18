import json
import re
from urllib.parse import urlencode

from paintwatch.domain import CardRef, FetchError, Snapshot, kopecks
from .structured import characteristics, parse_ld, same_card
from .web import json_data


class OzonAdapter:
    def __init__(self, web):
        self.web = web

    def parse(self, data, ref):
        widgets = data.get("widgetStates", {})
        sticky, prices, props = [], [], {}
        for key, raw in widgets.items():
            try:
                value = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            if key.startswith("webStickyProducts-"):
                sticky.append(value)
            if key.startswith("webPrice-"):
                prices.append(value)
            if key.startswith(("webCharacteristics-", "webShortCharacteristics-")):
                for group in value.get("characteristics", []):
                    if (
                        isinstance(group, dict)
                        and group.get("name")
                        and isinstance(group.get("value"), str)
                    ):
                        props[group["name"]] = group["value"]
                    elif isinstance(group, dict):
                        title = "".join(
                            part.get("content", "")
                            for part in group.get("title", {}).get("textRs", [])
                            if isinstance(part, dict)
                        )
                        values = "".join(
                            part.get("text", "")
                            for part in group.get("values", [])
                            if isinstance(part, dict)
                        )
                        if title and values:
                            props[title] = values
        matched = [
            p
            for p in sticky
            if str(p.get("sku", p.get("id", ""))) == ref.article
            or same_card(ref, p.get("link", p.get("url", "")))
        ]
        if len(matched) != 1 or not matched[0].get("name"):
            raise FetchError(
                "no_structured_data", "JSON Ozon не подтвердил SKU конкретной карточки"
            )
        snap = Snapshot(
            ref=ref,
            name=matched[0]["name"],
            source="Ozon composer JSON",
            price_kind="цена по карте на Ozon, RUB; зависит от доступности способа оплаты",
        )
        seller = matched[0].get("seller", {})
        seller_match = re.search(r"/seller/(?:[^/]*-)?(\d+)/?", seller.get("link", ""))
        if seller_match:
            snap.seller_id = seller_match[1]
        if seller.get("name"):
            props["Продавец"] = seller["name"]
        characteristics(snap, props)
        if len(prices) != 1:
            snap.issue = "Цена отсутствует или несколько ценовых виджетов"
            return snap
        value = prices[0]
        if value.get("isAvailable") is False:
            snap.issue = "Товар недоступен для покупки"
            return snap
        if value.get("currency", "RUB") not in ("RUB", "₽"):
            snap.issue = "Цена не в RUB"
            return snap
        # Правило владельца: отслеживаем cardPrice, обычная price — только справочно.
        regular = value.get("price")
        try:
            if isinstance(regular, str) and any(
                marker in regular for marker in ("₽", "руб", "RUB")
            ):
                snap.alternate_prices["Обычная цена"] = self.price_number(regular)
        except FetchError:
            pass
        price = value.get("cardPrice")
        if price is None:
            snap.issue = "Цена по карте не подтверждена; обычная цена не подставляется"
            return snap
        if not isinstance(price, str) or not any(
            marker in price for marker in ("₽", "руб", "RUB")
        ):
            snap.issue = "Единицы/валюта цены не подтверждены"
            return snap
        snap.price = self.price_number(price)
        return snap

    @staticmethod
    def price_number(price):
        for marker in ("руб.", "руб", "RUB"):
            price = price.replace(marker, "")
        return kopecks(price)

    @staticmethod
    def card_only_metadata(snapshot):
        # Общее поле Offer.price не подтверждает цену по карте.
        if snapshot.price is not None:
            snapshot.alternate_prices["Обычная цена"] = snapshot.price
        snapshot.price = None
        snapshot.price_kind = (
            "цена по карте на Ozon, RUB; зависит от доступности способа оплаты"
        )
        snapshot.issue = "Резервный источник подтвердил карточку, но не цену по карте"
        return snapshot

    async def fetch(self, ref: CardRef) -> Snapshot:
        url = "https://www.ozon.ru/api/composer-api.bx/page/json/v2?" + urlencode(
            {"url": f"/product/{ref.article}/"}
        )
        try:
            text, _ = await self.web.get("ozon", url)
            return self.parse(json_data(text), ref)
        except FetchError as error:
            if error.code not in (
                "schema_changed",
                "no_structured_data",
                "http_error",
                "not_found",
            ):
                raise
        html, final_url = await self.web.get("ozon", ref.url)
        try:
            return self.card_only_metadata(
                parse_ld(html, ref, final_url, "Ozon HTTP JSON-LD")
            )
        except FetchError as error:
            if error.code != "no_structured_data":
                raise
        html, final_url = await self.web.render("ozon", ref.url)
        return self.card_only_metadata(
            parse_ld(html, ref, final_url, "Ozon Playwright JSON-LD")
        )
