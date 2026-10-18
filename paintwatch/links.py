import re
from urllib.parse import parse_qs, urlencode, urlsplit

from .domain import CardRef, FetchError, Market

HOSTS = {
    "wildberries.ru": Market.WB,
    "www.wildberries.ru": Market.WB,
    "ozon.ru": Market.OZON,
    "www.ozon.ru": Market.OZON,
}


def extract_urls(text: str) -> tuple[list[str], list[str]]:
    # Lookahead отделяет следующий URL даже без пробела.
    pattern = r"https?://(?:(?!https?://)[^\s<>\"'`])+"
    matches = list(re.finditer(pattern, text, re.I))
    urls = [m.group().rstrip(".,;!)]}") for m in matches]
    remainder = re.sub(pattern, " ", text, flags=re.I).strip(" \n\t,;()[]")
    return urls, ([remainder[:200]] if remainder else [])


def parse_link(url: str) -> CardRef:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise ValueError
        market = HOSTS.get((parsed.hostname or "").lower())
        if market is None:
            raise ValueError
        qs = parse_qs(parsed.query)
        if market == Market.WB:
            m = re.fullmatch(r"/catalog/(\d+)/detail\.aspx/?", parsed.path)
            if not m:
                raise ValueError
            variant = qs.get("size", [""])[0]
            if variant and not variant.isdigit():
                raise ValueError
            article = m[1]
            canonical = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
            if variant:
                canonical += "?" + urlencode({"size": variant})
        elif market == Market.OZON:
            m = re.fullmatch(r"/product/(?:[^/]+-)?(\d+)/?", parsed.path)
            if not m:
                raise ValueError
            # SKU Ozon определяет конкретную карточку/вариант.
            article, variant = m[1], ""
            canonical = f"https://www.ozon.ru/product/{article}/"
        return CardRef(market, article, canonical, variant)
    except (ValueError, KeyError):
        raise FetchError(
            "invalid_link",
            "Нужна прямая ссылка на карточку WB или Ozon; сокращённые и поисковые ссылки не поддерживаются",
        ) from None


def article_ref(market: str, article: str) -> CardRef:
    article = article.strip()
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9_-]{1,100}", article):
        raise FetchError("invalid_article", "Некорректный формат артикула")
    if market not in ("wb", "ozon"):
        raise FetchError("invalid_market", "Неизвестная площадка")
    if not article.isdigit():
        raise FetchError(
            "ambiguous_article",
            "Буквенный артикул продавца не определяет карточку однозначно. Укажите числовой ID карточки или прямую ссылку",
        )
    url = (
        f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
        if market == "wb"
        else f"https://www.ozon.ru/product/{article}/"
    )
    return parse_link(url)
