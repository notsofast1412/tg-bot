import pytest

from scripts.wb_browser_probe import DEFAULT_URL, card_identity, public_product


def test_selected_card_and_size_are_retained():
    assert card_identity(DEFAULT_URL) == ('518397236', '716573024')


@pytest.mark.parametrize('url', ['https://wildberries.ru.evil.test/catalog/123/detail.aspx',
    'http://wildberries.ru/catalog/123/detail.aspx',
    'https://user:secret@wildberries.ru/catalog/123/detail.aspx',
    'https://wildberries.ru:443/catalog/123/detail.aspx',
    'https://wildberries.ru/catalog/123/detail.aspx?size=1&size=2',
    'https://wildberries.ru/catalog/123/detail.aspx?size=',
    'https://wildberries.ru/catalog/123/detail.aspx?size=bad'])
def test_unsafe_or_ambiguous_card_is_not_requested(url):
    with pytest.raises(ValueError):
        card_identity(url)


def test_only_exact_product_and_public_price_fields_are_exported():
    payload = {'token': 'PRIVATE', 'data': {'account': 'PRIVATE', 'products': [
        {'id': 999, 'name': 'recommendation', 'sizes': []},
        {'id': 123, 'name': 'Test', 'seller_token': 'PRIVATE', 'sizes': [
            {'chrtId': 456, 'user': 'PRIVATE', 'price': {'product': 26300, 'basic': 51600,
             'token': 'PRIVATE'}, 'stocks': [{'qty': 3, 'token': 'PRIVATE'}]}]}]}}
    result = public_product(payload, '123')
    assert result == {'id': '123', 'name': 'Test', 'sizes': [
        {'chrtId': 456, 'price': {'product': 26300, 'basic': 51600}, 'stock_qty': 3}]}
    assert 'PRIVATE' not in repr(result)
    assert public_product(payload, '777') is None
    payload['data']['products'].append(payload['data']['products'][1])
    assert public_product(payload, '123') is None


@pytest.mark.parametrize('payload', [None, [], {'data': None}, {'data': []}, {'products': []}])
def test_unrelated_or_invalid_json_is_not_a_price(payload):
    assert public_product(payload, '123') is None
