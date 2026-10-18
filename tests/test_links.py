import pytest

from paintwatch.domain import FetchError
from paintwatch.links import article_ref, extract_urls, parse_link


@pytest.mark.parametrize(
    "url,market,article,variant",
    [
        ("http://wildberries.ru/catalog/123/detail.aspx?utm_x=1", "wb", "123", ""),
        (
            "https://www.wildberries.ru/catalog/123/detail.aspx?size=987",
            "wb",
            "123",
            "987",
        ),
        ("https://ozon.ru/product/kraska-chernaya-456/?from=x", "ozon", "456", ""),
        ("https://www.ozon.ru/product/456/", "ozon", "456", ""),
    ],
)
def test_card_urls(url, market, article, variant):
    ref = parse_link(url)
    assert (ref.market, ref.article, ref.variant) == (market, article, variant)
    assert ref.url.startswith("https://")


@pytest.mark.parametrize(
    "url",
    [
        "https://wildberries.ru.evil.test/catalog/1/detail.aspx",
        "https://evilwildberries.ru/catalog/1/detail.aspx",
        "https://www.ozon.ru@localhost/product/1/",
        "https://www.ozon.ru:443/product/1/",
        "ftp://ozon.ru/product/1/",
        "https://127.0.0.1/product/1/",
        "https://ozon.ru/search/?text=paint",
        "https://www.wildberries.ru/catalog/0/search.aspx?search=paint",
        "https://market.yandex.ru/product--paint/123",
        "https://ozon.ru/t/short",
    ],
)
def test_unsafe_or_unsupported_links(url):
    with pytest.raises(FetchError):
        parse_link(url)


def test_adjacent_urls():
    a = "https://wildberries.ru/catalog/123/detail.aspx"
    b = "http://ozon.ru/product/456/"
    assert extract_urls(a + b)[0] == [a, b]
    assert extract_urls(a + "\n" + b)[0] == [a, b]


@pytest.mark.parametrize("market", ["wb", "ozon"])
def test_article_exact(market):
    assert article_ref(market, "123").article == "123"
    with pytest.raises(FetchError, match="не определяет"):
        article_ref(market, "ART-123")


def test_article_invalid():
    with pytest.raises(FetchError):
        article_ref("wb", "123/../../etc")
    assert article_ref("wb", "123").key != article_ref("ozon", "123").key
