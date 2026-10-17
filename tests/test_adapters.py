import json
from dataclasses import replace

import httpx
import pytest

from paintbot.marketplaces import WildberriesAdapter, OzonAdapter
from paintbot.marketplaces.links import MarketError, extract_links, from_article, parse_link

URLS = {"wb": "https://www.wildberries.ru/catalog/123/detail.aspx",
        "ozon": "https://www.ozon.ru/product/paint-123/"}
CLASSES = {"wb": WildberriesAdapter, "ozon": OzonAdapter}


def html(ref, **overrides):
    node = {"@type": "Product", "url": ref.url, "sku": "123", "name": "Краска", "color": "чёрный",
            "offers": {"@type": "Offer", "priceCurrency": "RUB", "price": "389.50", "availability": "https://schema.org/InStock"}}
    node.update(overrides)
    return '<script type="application/ld+json">' + json.dumps(node) + '</script>'


@pytest.mark.parametrize("market", URLS)
async def test_exact_html_and_article(market):
    ref = parse_link(URLS[market])
    assert ref.card_id == "123"
    assert from_article(market, "123") == ref
    adapter = CLASSES[market](gap=0)
    try:
        card = adapter.parse_html(ref, html(ref))
        assert card.price == 38950
        assert card.attributes["color"] == "чёрный"
        assert card.marketplace == market
        with pytest.raises(MarketError):
            adapter.parse_html(ref, html(ref, url=ref.url.replace("123", "456"), sku="456"))
    finally:
        await adapter.client.aclose()


@pytest.mark.parametrize("url", ["https://evil.com/product/123", "https://ozon.ru.evil.com/product/123",
    "https://www.ozon.ru@127.0.0.1/product/123", "https://www.ozon.ru:443/product/123/",
    "http://localhost/", "file:///etc/passwd", "https://market.yandex.ru/search?text=paint",
    "https://www.wildberries.ru/catalog/123/detail.aspx?size=bad"])
def test_reject_unsafe_or_ambiguous_links(url):
    with pytest.raises(MarketError):
        parse_link(url)


def test_adjacent_links():
    assert len(extract_links(URLS["wb"] + URLS["ozon"])) == 2
    assert extract_links("не ссылка") == []


def test_seller_article_not_fuzzy_searched():
    with pytest.raises(MarketError):
        from_article("ozon", "PAINT-BLACK")


@pytest.mark.parametrize("offers", [
    {"@type": "AggregateOffer", "lowPrice": 1, "priceCurrency": "RUB"},
    [{"price": 1}, {"price": 2}],
    {"price": 1, "priceCurrency": "USD", "availability": "https://schema.org/InStock"},
    {"price": 1, "priceCurrency": "RUB", "availability": "https://schema.org/OutOfStock"},
    {"price": "NaN", "priceCurrency": "RUB", "availability": "https://schema.org/InStock"},
    {"price": 1, "priceCurrency": "RUB", "availability": "https://schema.org/InStock", "priceValidUntil": "2020-01-01"},
    {"price": 1, "priceCurrency": "RUB", "availability": "https://schema.org/InStock", "url": "https://www.ozon.ru/product/456/"},
])
async def test_ambiguous_prices_are_pending(offers):
    adapter = OzonAdapter()
    ref = from_article("ozon", "123")
    try:
        card = adapter.parse_html(ref, html(ref, offers=offers))
        assert card.price is None and card.warning
    finally:
        await adapter.client.aclose()


async def test_wb_available_size_price():
    adapter = WildberriesAdapter()
    ref = from_article("wb", "123")
    data = {"data": {"products": [{"id": 123, "name": "Краска", "sizes": [
        {"stocks": [{"qty": 1}], "price": {"product": 39900}}]}]}}
    try:
        assert adapter.parse_api(ref, data).price == 39900
        data["data"]["products"][0]["sizes"].append({"stocks": [{"qty": 1}], "price": {"product": 50000}})
        assert adapter.parse_api(ref, data).price is None
        with pytest.raises(MarketError):
            adapter.parse_api(from_article("wb", "456"), data)
    finally:
        await adapter.client.aclose()


@pytest.mark.parametrize("status", [403, 429, 302, 404])
async def test_access_failures_no_follow_redirects(status):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, headers={"Location": "http://127.0.0.1/"})))
    adapter = OzonAdapter(client=client, gap=0)
    try:
        with pytest.raises(MarketError):
            await adapter.fetch(from_article("ozon", "123"))
    finally:
        await client.aclose()


async def test_http_method_and_stale_cache():
    ref = from_article("ozon", "123")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html(ref))))
    try:
        assert (await OzonAdapter(client=client, gap=0).fetch(ref)).price == 38950
    finally:
        await client.aclose()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html(ref), headers={"Age": "1000"})))
    try:
        with pytest.raises(MarketError, match="кэша"):
            await OzonAdapter(client=client, gap=0).fetch(ref)
    finally:
        await client.aclose()


async def test_wb_html_fallback():
    ref = from_article("wb", "123")
    def respond(request):
        return httpx.Response(200, json={}) if request.url.host == "card.wb.ru" else httpx.Response(200, text=html(ref))
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        assert (await WildberriesAdapter(client=client, gap=0).fetch(ref)).price == 38950
    finally:
        await client.aclose()
