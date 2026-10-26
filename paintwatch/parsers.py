import json
import re

from .domain import PriceError, Snapshot, money


def block_page(text):
    sample = text[:200000].lower()
    if "/__wbaas/challenges/antibot/" in sample:
        raise PriceError(
            "Wildberries требует ручную проверку в браузере", code="access"
        )
    if ("<html" in sample or "<!doctype" in sample) and "fab_chlg_" in sample:
        raise PriceError(
            "Ozon показал ошибку проверки доступа (fab_chlg). Доступ к цене не подтверждён",
            code="access",
        )
    if any(
        marker in sample
        for marker in (
            "<title>доступ ограничен",
            "<title>access denied",
            "<title>robot or human",
            "<title>just a moment",
            "<title>проверка браузера",
            "captcha-container",
            'id="captcha"',
            "showcaptcha",
            "challenge-platform",
        )
    ):
        raise PriceError("Пройдите проверку доступа в открытом браузере", code="access")


def as_json(text):
    block_page(text)
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except (TypeError, ValueError):
        pass
    raise PriceError("Источник не вернул ожидаемые данные карточки", code="schema")


def parse_ozon(data, ref):
    sticky, prices, props = [], [], {}
    for key, raw in data.get("widgetStates", {}).items():
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if (
            key.startswith("webStickyProducts-")
            and str(value.get("sku", value.get("id", ""))) == ref.article
        ):
            sticky.append(value)
        if key.startswith("webPrice-"):
            prices.append(value)
        if key.startswith(("webCharacteristics-", "webShortCharacteristics-")):
            for item in value.get("characteristics", []):
                if not isinstance(item, dict):
                    continue
                title = item.get("name") or "".join(
                    p.get("content", "")
                    for p in item.get("title", {}).get("textRs", [])
                    if isinstance(p, dict)
                )
                value_text = item.get("value") or "".join(
                    p.get("text", "")
                    for p in item.get("values", [])
                    if isinstance(p, dict)
                )
                if isinstance(title, str) and isinstance(value_text, str):
                    props[title.lower()] = value_text
    if len(sticky) != 1 or not sticky[0].get("name"):
        raise PriceError("Ozon не подтвердил SKU конкретного товара", code="identity")
    seller = sticky[0].get("seller", {})
    seller_id = re.search(r"/seller/(?:[^/]*-)?(\d+)/?", seller.get("link", ""))
    result = Snapshot(
        ref,
        sticky[0]["name"],
        None,
        seller=seller_id[1] if seller_id else "",
        color=props.get("цвет", ""),
        volume=props.get("объем", props.get("объём", "")),
        pack=props.get("количество в упаковке", ""),
        source="Ozon: цена по карте, браузер",
    )
    if len(prices) != 1 or prices[0].get("isAvailable") is False:
        result.issue = "Цена отсутствует, неоднозначна или товар недоступен"
    else:
        price = prices[0].get("cardPrice")
        if prices[0].get("currency", "RUB") not in ("RUB", "₽"):
            result.issue = "Валюта не RUB"
        elif not isinstance(price, str) or not any(
            s in price for s in ("₽", "руб", "RUB")
        ):
            result.issue = (
                "Цена по карте не подтверждена; обычная цена не подставляется"
            )
        else:
            for suffix in ("руб.", "руб", "RUB"):
                price = price.replace(suffix, "")
            try:
                result.price = money(price)
            except ValueError:
                result.issue = "Ozon вернул некорректную цену по карте"
    return result


def parse_wb(data, ref):
    products = data.get("products", data.get("data", {}).get("products", []))
    matches = [
        p
        for p in products
        if isinstance(p, dict) and str(p.get("id", p.get("nmId", ""))) == ref.article
    ]
    if len(matches) != 1 or not matches[0].get("name"):
        raise PriceError("WB не подтвердил конкретную карточку", code="identity")
    item = matches[0]
    candidates = [
        s
        for s in item.get("sizes", [])
        if not ref.variant or str(s.get("optionId", "")) == ref.variant
    ]
    result = Snapshot(
        ref,
        item["name"],
        None,
        seller=str(item.get("supplierId", "")),
        source="WB: цена товара без WB Кошелька, браузер",
    )
    if len(candidates) != 1:
        result.issue = (
            "Несколько размеров или размер не найден. Добавьте ссылку с ?size=optionId"
        )
        return result
    size = candidates[0]
    stocks = size.get("stocks")
    if isinstance(stocks, list) and not any(
        isinstance(s.get("qty"), int) and s["qty"] > 0 for s in stocks
    ):
        result.issue = "Нет подтверждённого остатка"
        return result
    price = size.get("price", {}).get("product")
    if type(price) is not int or not 0 < price <= 10**10:
        result.issue = "Текущая цена не подтверждена"
    elif size.get("price", {}).get("currency", "RUB") not in ("RUB", 643):
        result.issue = "Валюта не RUB"
    else:
        result.price = price
    return result
