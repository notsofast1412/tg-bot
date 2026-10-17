import httpx
import pytest
from unittest.mock import AsyncMock

from paintbot.marketplaces import WildberriesAdapter
from paintbot.marketplaces.links import MarketError, parse_link


URL = "https://www.wildberries.ru/catalog/29220652/detail.aspx?size=65383223"


def payload():
    # Synthetic fixture, not a claim about this real card's name or price.
    return {"data": {"products": [{"id": 29220652, "name": "Тестовая краска", "sizes": [
        {"chrtId": 65383223, "name": "Вариант A", "stocks": [{"qty": 2}], "price": {"product": 50000}},
        {"chrtId": 65383224, "name": "Вариант B", "stocks": [{"qty": 2}], "price": {"product": 10000}},
    ]}]}}


def test_wb_size_is_part_of_identity_and_canonical_url():
    ref = parse_link(URL + "&utm_source=share")
    assert ref.card_id == "29220652|size:65383223"
    assert ref.url == URL
    assert parse_link(URL.replace("65383223", "65383224")) != ref
    assert parse_link(URL.split("?")[0]) != ref


@pytest.mark.parametrize("query", ["size=", "size=0", "size=abc", "size=1&size=2", "size=1&size=1", "size=-1"])
def test_invalid_size(query):
    with pytest.raises(MarketError):
        parse_link(URL.split("?")[0] + "?" + query)


async def test_exact_variant_price_not_other_cheaper_variant():
    async with httpx.AsyncClient() as client:
        card = WildberriesAdapter(client=client).parse_api(parse_link(URL), payload())
        assert card.price == 50000
        assert card.article == "29220652"
        assert card.card_id == "29220652|size:65383223"
        assert card.attributes["Вариант WB"]["name"] == "Вариант A"


@pytest.mark.parametrize("case", ["missing", "duplicate", "out_of_stock"])
async def test_unconfirmed_or_unavailable_size_never_uses_other_variant(case):
    data = payload()
    sizes = data["data"]["products"][0]["sizes"]
    if case == "missing":
        sizes[0]["chrtId"] = 999
    elif case == "duplicate":
        sizes.append(dict(sizes[0]))
    else:
        sizes[0]["stocks"] = [{"qty": 0}]
    async with httpx.AsyncClient() as client:
        card = WildberriesAdapter(client=client).parse_api(parse_link(URL), data)
        assert card.price is None and card.warning


async def test_browser_request_preserves_the_selected_card_and_variant():
    report = {'schema': 1, 'page_http': 200, 'same_card_and_size': True,
              'card_id': '29220652', 'size': '65383223',
              'products': [{'id': '29220652', 'name': 'Краска', 'sizes': [
                  {'optionId': 65383223, 'stock_qty': 2, 'price': {'product': 50000}},
                  {'optionId': 65383224, 'stock_qty': 2, 'price': {'product': 10000}}]}]}
    fetch = AsyncMock(return_value=report)
    async with httpx.AsyncClient() as client:
        card = await WildberriesAdapter(client=client, gap=0, browser_fetch=fetch).fetch(parse_link(URL))
        fetch.assert_awaited_once_with(URL)
        assert card.card_id == "29220652|size:65383223"
        assert card.price == 50000


async def test_html_without_exact_variant_url_is_not_used():
    from tests.test_adapters import html
    base = parse_link(URL.split("?")[0])
    async with httpx.AsyncClient() as client:
        with pytest.raises(MarketError):
            WildberriesAdapter(client=client).parse_html(parse_link(URL), html(base, sku="29220652"))


@pytest.mark.parametrize('wrapper', [True, False])
async def test_option_id_and_single_variant_total_stock(wrapper):
    product = {'id': 29220652, 'name': 'Краска', 'totalQuantity': 1,
               'sizes': [{'optionId': 65383223, 'price': {'product': 26300, 'basic': 51600}}]}
    data = {'products': [product]}
    if wrapper:
        data = {'data': data}
    async with httpx.AsyncClient() as client:
        card = WildberriesAdapter(client=client).parse_api(parse_link(URL), data)
        assert card.card_id == '29220652|size:65383223'
        assert card.price == 26300


@pytest.mark.parametrize('case', ['wrong_size', 'conflicting_ids', 'multiple_sizes', 'empty_stocks',
                                  'out_of_stock', 'unknown_stock', 'invalid_stock'])
async def test_new_schema_never_infers_unconfirmed_variant_stock(case):
    size = {'optionId': 65383223, 'price': {'product': 26300}}
    product = {'id': 29220652, 'name': 'Краска', 'totalQuantity': 1, 'sizes': [size]}
    if case == 'wrong_size':
        size['optionId'] = 999
    elif case == 'conflicting_ids':
        size['chrtId'] = 999
    elif case == 'multiple_sizes':
        product['sizes'].append({'optionId': 999, 'price': {'product': 10000}})
    elif case == 'empty_stocks':
        size['stocks'] = []
    elif case == 'out_of_stock':
        product['totalQuantity'] = 0
    elif case == 'unknown_stock':
        del product['totalQuantity']
    else:
        product['totalQuantity'] = True
    async with httpx.AsyncClient() as client:
        card = WildberriesAdapter(client=client).parse_api(parse_link(URL), {'products': [product]})
        assert card.price is None and card.warning
