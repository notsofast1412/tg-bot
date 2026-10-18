import json
import re

from bs4 import BeautifulSoup

from paintwatch.domain import FetchError, Snapshot, kopecks
from paintwatch.links import parse_link


def same_card(ref, url):
    try:
        other = parse_link(url)
        return (
            other.market == ref.market
            and other.article == ref.article
            and (not ref.variant or other.variant == ref.variant)
        )
    except FetchError:
        return False


def characteristics(snapshot: Snapshot, values: dict):
    snapshot.characteristics = {
        str(k)[:200]: str(v)[:500] for k, v in values.items() if v is not None
    }
    for key, value in snapshot.characteristics.items():
        normalized = key.strip().lower()
        if normalized in ("цвет", "цвет товара", "color"):
            snapshot.color = value
        if normalized in ("объем", "объём", "объем товара", "объём товара", "volume"):
            snapshot.volume = value
        if normalized in (
            "количество в упаковке",
            "количество единиц в упаковке",
            "numberofitems",
        ):
            match = re.fullmatch(r"\s*(\d+)\s*(?:шт\.?|штук)?\s*", value)
            if match and 0 < int(match[1]) < 10000:
                snapshot.pack_count = int(match[1])


def parse_ld(html, ref, final_url, source="JSON-LD") -> Snapshot:
    if not same_card(ref, final_url):
        raise FetchError(
            "identity_mismatch", "После перехода открылась другая карточка/вариант"
        )
    soup = BeautifulSoup(html, "html.parser")
    nodes = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        nodes.extend(data if isinstance(data, list) else [data])
    products = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for item in [node] + (
            node.get("@graph", []) if isinstance(node.get("@graph"), list) else []
        ):
            types = item.get("@type", []) if isinstance(item, dict) else []
            types = types if isinstance(types, list) else [types]
            if isinstance(item, dict) and "Product" in types:
                products.append(item)
    matched = []
    for item in products:
        ids = [str(item.get(k, "")) for k in ("sku", "productID")]
        urls = [item.get("url"), item.get("@id")]
        if ref.article in ids or any(
            isinstance(u, str) and same_card(ref, u) for u in urls
        ):
            matched.append(item)
    if len(matched) != 1 or not matched[0].get("name"):
        raise FetchError(
            "no_structured_data",
            "Не найдены однозначные структурированные данные указанной карточки",
        )
    item = matched[0]
    snap = Snapshot(ref=ref, name=str(item["name"]), source=source)
    props = {
        p["name"]: p.get("value")
        for p in item.get("additionalProperty", [])
        if isinstance(p, dict) and p.get("name")
    }
    for key in ("color", "numberOfItems"):
        if key in item:
            props[key] = item[key]
    characteristics(snap, props)
    offers = item.get("offers", [])
    offers = offers if isinstance(offers, list) else [offers]
    if (
        len(offers) != 1
        or not isinstance(offers[0], dict)
        or offers[0].get("@type") == "AggregateOffer"
    ):
        snap.issue = (
            "Несколько предложений/агрегированная цена; фактическая цена неоднозначна"
        )
        return snap
    offer = offers[0]
    if offer.get("url") and not same_card(ref, offer["url"]):
        raise FetchError("identity_mismatch", "Предложение относится к другой карточке")
    if offer.get("availability", "").rsplit("/", 1)[-1] in (
        "OutOfStock",
        "Discontinued",
        "PreOrder",
        "SoldOut",
    ):
        snap.issue = "Товар недоступен для покупки"
        return snap
    if offer.get("priceCurrency") != "RUB":
        snap.issue = "Валюта цены не подтверждена как RUB"
        return snap
    if offer.get("priceValidUntil"):
        from datetime import date

        try:
            if date.fromisoformat(offer["priceValidUntil"][:10]) < date.today():
                snap.issue = "Срок действия цены истёк"
                return snap
        except (ValueError, TypeError):
            snap.issue = "Невозможно проверить срок действия цены"
            return snap
    if offer.get("price") is not None:
        snap.price = kopecks(offer["price"])
    else:
        snap.issue = "Фактическая цена отсутствует"
    return snap
