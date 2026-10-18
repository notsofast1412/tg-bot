from urllib.parse import urlencode

from paintwatch.domain import CardRef, FetchError, Snapshot
from .structured import characteristics, parse_ld
from .web import json_data


class WildberriesAdapter:
    def __init__(self, web, dest="-1257786"):
        self.web, self.dest = web, dest

    def parse(self, data, ref):
        products = data.get("products", data.get("data", {}).get("products", []))
        found = [
            p for p in products if str(p.get("id", p.get("nmId", ""))) == ref.article
        ]
        if len(found) != 1 or not found[0].get("name"):
            raise FetchError(
                "identity_mismatch",
                "API WB не подтвердил ID и название указанной карточки",
            )
        p = found[0]
        snap = Snapshot(
            ref=ref,
            name=p["name"],
            source="WB card API v4",
            price_kind=f"обычная цена RUB, регион dest={self.dest}, без WB Кошелька",
        )
        if p.get("supplierId") is not None:
            snap.seller_id = str(p["supplierId"])
        props = {"Бренд": p.get("brand")}
        colors = p.get("colors", [])
        if len(colors) == 1 and isinstance(colors[0], dict):
            props["Цвет"] = colors[0].get("name")
        characteristics(snap, props)
        sizes = p.get("sizes", [])
        if ref.variant:
            sizes = [s for s in sizes if str(s.get("optionId")) == ref.variant]
        if len(sizes) != 1:
            snap.issue = "Нужен выбранный размер (?size=optionId); вариант отсутствует или неоднозначен"
            return snap
        size = sizes[0]
        snap.characteristics["Вариант"] = str(
            size.get("name", size.get("origName", ""))
        )
        stocks = size.get("stocks")
        if (
            isinstance(stocks, list) and not any(s.get("qty", 0) > 0 for s in stocks)
        ) or p.get("totalQuantity") == 0:
            snap.issue = "Товар отсутствует в продаже"
            return snap
        price = size.get("price", {}).get("product")
        # API отдаёт копейки; не подменяем текущую цену basic/priceU.
        if (
            isinstance(price, int)
            and not isinstance(price, bool)
            and 0 < price <= 10_000_000_000
        ):
            snap.price = price
        else:
            snap.issue = "В API нет подтверждённой текущей цены product"
        return snap

    async def fetch(self, ref: CardRef) -> Snapshot:
        url = "https://card.wb.ru/cards/v4/detail?" + urlencode(
            {"appType": 1, "curr": "rub", "dest": self.dest, "nm": ref.article}
        )
        try:
            text, _ = await self.web.get("wb", url)
            return self.parse(json_data(text), ref)
        except FetchError as error:
            if error.code not in ("schema_changed", "http_error", "no_structured_data"):
                raise
        html, final_url = await self.web.get("wb", ref.url)
        try:
            return parse_ld(html, ref, final_url, "WB HTTP JSON-LD")
        except FetchError as error:
            if error.code != "no_structured_data":
                raise
        html, final_url = await self.web.render("wb", ref.url)
        return parse_ld(html, ref, final_url, "WB Playwright JSON-LD")
