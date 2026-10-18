import json
from unittest.mock import AsyncMock

import httpx
import pytest

from paintwatch.domain import FetchError
from paintwatch.links import article_ref, parse_link
from paintwatch.marketplaces.ozon import OzonAdapter
from paintwatch.marketplaces.structured import parse_ld
from paintwatch.marketplaces.wb import WildberriesAdapter
from paintwatch.marketplaces.web import Web


def wb_json(article=123456):
    return {
        "products": [
            {
                "id": article,
                "name": "Краска WB",
                "brand": "ARTON",
                "volume": 400,
                "totalQuantity": 5,
                "sizes": [
                    {
                        "optionId": 987,
                        "name": "0",
                        "price": {"basic": 90000, "product": 38900},
                        "stocks": [{"qty": 5}],
                    }
                ],
            }
        ]
    }


def ozon_json():
    return {
        "widgetStates": {
            "webStickyProducts-1": json.dumps(
                {"sku": 123456, "name": "Краска Ozon", "seller": {"id": 22}}
            ),
            "webPrice-1": json.dumps(
                {"price": "420 ₽", "cardPrice": "389 ₽", "originalPrice": "999 ₽"}
            ),
        }
    }


def ld(ref, **offer_changes):
    offer = {
        "@type": "Offer",
        "price": "389.50",
        "priceCurrency": "RUB",
        "availability": "https://schema.org/InStock",
        "url": ref.url,
    }
    offer.update(offer_changes)
    product = {
        "@type": "Product",
        "name": "Краска",
        "sku": ref.article,
        "url": ref.url,
        "offers": offer,
        "additionalProperty": [
            {"name": "Объём", "value": "400 мл"},
            {"name": "Количество в упаковке", "value": "6 шт"},
        ],
    }
    return '<script type="application/ld+json">' + json.dumps(product) + "</script>"


async def test_wb_api_fetch(config, ref):
    web = AsyncMock()
    web.get.return_value = (json.dumps(wb_json()), "https://card.wb.ru/cards/v4/detail")
    snap = await WildberriesAdapter(web).fetch(ref)
    assert snap.price == 38900 and snap.ref == ref
    assert (
        snap.pack_count is None and snap.volume is None
    )  # volume API не является остатком/объёмом упаковки.
    web.render.assert_not_called()


def test_wb_different_card_is_not_substituted(ref):
    with pytest.raises(FetchError, match="ID"):
        WildberriesAdapter(None).parse(wb_json(999), ref)


def test_wb_selected_variant_and_stock(ref):
    adapter = WildberriesAdapter(None)
    payload = wb_json()
    payload["products"][0]["sizes"].append(
        {"optionId": 111, "price": {"product": 10000}}
    )
    assert adapter.parse(payload, ref).price is None
    selected = parse_link(ref.url + "?size=987")
    assert adapter.parse(payload, selected).price == 38900
    payload["products"][0]["totalQuantity"] = 0
    assert adapter.parse(payload, selected).price is None


def test_wb_never_falls_back_to_old_price(ref):
    payload = wb_json()
    del payload["products"][0]["sizes"][0]["price"]["product"]
    assert WildberriesAdapter(None).parse(payload, ref).price is None


async def test_ozon_fetch_card_price_preferred():
    ref = article_ref("ozon", "123456")
    web = AsyncMock()
    web.get.return_value = (
        json.dumps(ozon_json()),
        "https://www.ozon.ru/api/composer-api.bx/page/json/v2",
    )
    s = await OzonAdapter(web).fetch(ref)
    assert s.price == 38900 and s.alternate_prices["Обычная цена"] == 42000
    payload = ozon_json()
    payload["widgetStates"]["webPrice-1"] = json.dumps({"price": "100 ₽"})
    assert OzonAdapter(web).parse(payload, ref).price is None


def test_ozon_requires_exact_sku():
    with pytest.raises(FetchError):
        OzonAdapter(None).parse(ozon_json(), article_ref("ozon", "999"))


def test_captured_real_ozon_schema_and_unavailable_price():
    from pathlib import Path

    data = json.loads(
        (Path(__file__).parent / "fixtures" / "ozon-196525150.json").read_text()
    )
    adapter = OzonAdapter(None)
    snapshot = adapter.parse(data, article_ref("ozon", "196525150"))
    assert (
        snapshot.price == 65500
        and snapshot.color == "Nuts коричнево-красный"
        and snapshot.seller_id == "38248"
    )
    assert (
        snapshot.volume is None and snapshot.pack_count is None
    )  # Не выводим из названия/тега.
    key = next(k for k in data["widgetStates"] if k.startswith("webPrice-"))
    value = json.loads(data["widgetStates"][key])
    value["isAvailable"] = False
    data["widgetStates"][key] = json.dumps(value)
    assert adapter.parse(data, snapshot.ref).price is None


def test_price_unicode_thousands():
    from paintwatch.domain import kopecks

    assert kopecks("1\u2009260\u2009₽") == 126000


@pytest.mark.parametrize("market", ["wb", "ozon"])
def test_ld_json_and_characteristics(market):
    ref = article_ref(market, "123456")
    snap = parse_ld(ld(ref), ref, ref.url)
    assert snap.price == 38950 and snap.volume == "400 мл" and snap.pack_count == 6


@pytest.mark.parametrize(
    "changes",
    [
        {"priceCurrency": "USD"},
        {"availability": "https://schema.org/OutOfStock"},
        {"@type": "AggregateOffer"},
        {"priceValidUntil": "2000-01-01"},
        {"price": None},
    ],
)
def test_ld_unreliable_price_remains_unknown(ref, changes):
    assert parse_ld(ld(ref, **changes), ref, ref.url).price is None


def test_ld_rejects_recommendations_and_redirect(ref):
    wrong = article_ref("wb", "999")
    with pytest.raises(FetchError):
        parse_ld(ld(wrong), ref, ref.url)
    with pytest.raises(FetchError):
        parse_ld(ld(ref), ref, wrong.url)


@pytest.mark.parametrize(
    "adapter_type,market", [(WildberriesAdapter, "wb"), (OzonAdapter, "ozon")]
)
async def test_http_html_fallback(adapter_type, market):
    ref = article_ref(market, "123456")
    web = AsyncMock()
    web.get.side_effect = [("<html>not JSON</html>", ref.url), (ld(ref), ref.url)]
    snapshot = await adapter_type(web).fetch(ref)
    assert snapshot.price == (38950 if market == "wb" else None)
    if market == "ozon":
        assert snapshot.alternate_prices["Обычная цена"] == 38950


@pytest.mark.parametrize(
    "adapter_type,market", [(WildberriesAdapter, "wb"), (OzonAdapter, "ozon")]
)
async def test_browser_fallback_only_when_no_data(adapter_type, market):
    ref = article_ref(market, "123456")
    web = AsyncMock()
    web.get.side_effect = [("not JSON", ref.url), ("<html>JS page</html>", ref.url)]
    web.render.return_value = (ld(ref), ref.url)
    snap = await adapter_type(web).fetch(ref)
    assert (
        snap.price == (38950 if market == "wb" else None)
        and "Playwright" in snap.source
    )


async def test_does_not_try_browser_after_access_denial(ref):
    web = AsyncMock()
    web.get.side_effect = FetchError("access_blocked", "CAPTCHA")
    with pytest.raises(FetchError):
        await WildberriesAdapter(web).fetch(ref)
    web.render.assert_not_called()
    assert web.get.call_count == 1


async def test_real_http_transport_mock_schema_and_safe_redirect(config):
    def handle(request):
        assert request.headers["cache-control"] == "no-cache"
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    web = Web(config, client)
    with pytest.raises(FetchError) as result:
        await web.get("ozon", "https://www.ozon.ru/product/123/")
    assert result.value.code == "unsafe_redirect"
    await web.close()


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_access_failure_no_retries_and_cooldown(config, status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status)

    web = Web(config, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(FetchError) as result:
        await web.get("ozon", "https://www.ozon.ru/product/123/")
    assert result.value.code == "access_blocked"
    with pytest.raises(FetchError) as result:
        await web.get("ozon", "https://www.ozon.ru/product/123/")
    assert result.value.code == "cooldown" and len(calls) == 1
    await web.close()


async def test_cached_response_is_not_fresh_price(config):
    web = Web(
        config,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, headers={"Age": "30"}, text="{}")
            )
        ),
    )
    with pytest.raises(FetchError) as result:
        await web.get("wb", "https://card.wb.ru/cards/v4/detail")
    assert result.value.code == "stale_data"
    await web.close()


async def test_retry_after_is_persisted_and_honored_after_restart(
    config, store, product
):
    transport = httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "3600"})
    )
    web = Web(config, httpx.AsyncClient(transport=transport), store=store)
    with pytest.raises(FetchError) as result:
        await web.get("wb", "https://card.wb.ru/cards/v4/detail")
    assert result.value.retry_after == 3600
    store.apply(product, None, result.value, 600)
    from paintwatch.domain import timestamp

    assert store.get_product(product)["next_check"] >= timestamp() + 3599
    await web.close()
    new_web = Web(
        config,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: pytest.fail("cooldown must prevent request")
            )
        ),
        store=store,
    )
    with pytest.raises(FetchError) as result:
        await new_web.get("wb", "https://card.wb.ru/cards/v4/detail")
    assert result.value.code == "cooldown"
    await new_web.close()


@pytest.mark.parametrize("status", [200, 498])
async def test_wbaas_detected_before_data_parsing_and_without_retry(config, status):
    html = '<html><title>...</title><script src="/__wbaas/challenges/antibot/__static/v2/browser-check.js"></script></html>'
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, text=html)

    web = Web(config, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(FetchError) as result:
        await web.get("wb", "https://www.wildberries.ru/catalog/29220564/detail.aspx")
    assert result.value.code == "access_blocked"
    assert "WBAAS AntiBot" in str(result.value)
    assert result.value.retry_after >= 600 and len(calls) == 1
    await web.close()


async def test_empty_403_does_not_claim_origin_or_expired_token(config):
    web = Web(
        config,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))),
    )
    with pytest.raises(FetchError) as result:
        await web.get("wb", "https://card.wb.ru/cards/v4/detail")
    assert result.value.code == "access_blocked"
    assert "не установлен" in str(result.value)
    await web.close()


async def test_explicit_proxy_denial_distinguished_from_marketplace(config):
    web = Web(
        config,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    403, headers={"x-proxy-error": "policy_denied"}
                )
            )
        ),
    )
    with pytest.raises(FetchError) as result:
        await web.get("wb", "https://card.wb.ru/cards/v4/detail")
    assert result.value.code == "proxy_denied"
    assert result.value.retry_after >= 600
    await web.close()


async def test_browser_tls_failure_has_specific_safe_diagnosis(config, monkeypatch):
    from unittest.mock import MagicMock
    import playwright.async_api as api

    config.browser_enabled = True
    context = MagicMock()
    context.__aenter__ = AsyncMock(
        side_effect=api.Error(
            "net::ERR_CERT_AUTHORITY_INVALID: sensitive exception detail"
        )
    )
    monkeypatch.setattr(api, "async_playwright", lambda: context)
    web = Web(config)
    with pytest.raises(FetchError) as result:
        await web.render(
            "wb", "https://www.wildberries.ru/catalog/29220564/detail.aspx"
        )
    assert result.value.code == "tls_untrusted"
    assert "NSS" in str(result.value)
    assert "sensitive exception detail" not in str(result.value)
    await web.close()
