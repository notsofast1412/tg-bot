import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit


class MarketError(ValueError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class CardRef:
    marketplace: str
    card_id: str
    url: str


HOSTS = {"wildberries.ru": "wb", "www.wildberries.ru": "wb",
         "ozon.ru": "ozon", "www.ozon.ru": "ozon",
         "market.yandex.ru": "yandex"}


def is_ozon_short(url):
    return bool(re.fullmatch(r"https?://(?:www\.)?ozon\.ru/t/[A-Za-z0-9]+/?(?:\?[^\s#]*)?(?:#[^\s]*)?", url))


def is_yandex_short(url):
    return bool(re.fullmatch(r"https?://market\.yandex\.ru/cc/[A-Za-z0-9]+/?(?:\?[^\s#]*)?(?:#[^\s]*)?", url))


async def resolve_link(url, adapters):
    try:
        return parse_link(url)
    except MarketError:
        if is_ozon_short(url):
            return await adapters["ozon"].resolve_short(url)
        if is_yandex_short(url):
            return await adapters["yandex"].resolve_short(url)
        raise


def extract_links(text):
    # A new scheme terminates the previous link even without whitespace.
    return [m.group().rstrip(".,;!)]}>") for m in re.finditer(
        r"https?://(?:(?!https?://)[^\s<>\"'])+", text, re.I)]


def parse_link(url: str) -> CardRef:
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.username or parts.password or parts.port:
            raise ValueError()
        market = HOSTS.get(parts.hostname)
        if not market:
            raise ValueError()
    except ValueError as exc:
        raise MarketError("Нужна прямая ссылка WB, Ozon или market.yandex.ru без логина и порта") from exc
    path = parts.path.rstrip("/")
    query = parse_qs(parts.query, keep_blank_values=True)
    if market == "wb":
        match = re.fullmatch(r"/catalog/(\d+)/detail\.aspx", path)
        canonical = f"https://www.wildberries.ru/catalog/{match[1]}/detail.aspx" if match else ""
        card_id = match[1] if match else ""
        if "size" in query:
            selector = query["size"]
            if len(selector) != 1 or not re.fullmatch(r"[1-9]\d{0,19}", selector[0]):
                raise MarketError("Некорректный или неоднозначный идентификатор size WB")
            if card_id:
                card_id += "|size:" + selector[0]
                canonical += "?size=" + selector[0]
    elif market == "ozon":
        match = re.fullmatch(r"/product/(?:[^/]*-)?(\d+)", path)
        card_id = match[1] if match else ""
        canonical = f"https://www.ozon.ru/product/{card_id}/" if match else ""
    else:
        match = re.fullmatch(r"/(?:product--[^/]+|product)/(\d+)", path)
        offer = re.fullmatch(r"/offer/([A-Za-z0-9_-]+)", path)
        card = re.fullmatch(r"/card/[^/]+/(\d+)", path)
        if card:
            card_id = "card:" + card[1]
            canonical = "https://market.yandex.ru" + path
        elif offer:
            card_id = "offer:" + offer[1]
            canonical = f"https://market.yandex.ru/offer/{offer[1]}"
        else:
            card_id = match[1] if match else ""
            canonical = f"https://market.yandex.ru/product/{card_id}" if match else ""
        # Yandex share URLs can use both names for the same specific offer.
        if "do-waremd5" in query:
            if "offerid" in query and query["offerid"] != query["do-waremd5"]:
                raise MarketError("Конфликт идентификаторов предложения offerid и do-waremd5")
            query["offerid"] = query["do-waremd5"]
        selectors = ("sku", "uniqueId", "offerid", "publicId")
        # Preserve variant and seller selection in both identity and URL.
        for key in selectors:
            if query.get(key):
                value = query[key]
                if len(value) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value[0]):
                    raise MarketError("Некорректный идентификатор варианта/продавца")
                if card_id:
                    card_id += f"|{key}:{value[0]}"
        selections = {k: query[k][0] for k in selectors if query.get(k)}
        if selections:
            canonical += "?" + urlencode(selections)
    if not card_id:
        raise MarketError("Не распознана карточка. Нужна прямая ссылка, Ozon /t/ или Яндекс /cc/ без постороннего текста")
    return CardRef(market, card_id, canonical)


def from_article(market: str, article: str) -> CardRef:
    article = article.strip()
    if market == "yandex" and re.fullmatch(r"[A-Za-z0-9_-]{4,100}", article) and not article.isdigit():
        return parse_link(f"https://market.yandex.ru/offer/{article}")
    if not re.fullmatch(r"[1-9]\d{0,19}", article):
        raise MarketError("WB и Ozon: числовой ID карточки. Для артикула продавца нет однозначного разрешения; добавьте прямую ссылку")
    templates = {"wb": "https://www.wildberries.ru/catalog/{}/detail.aspx",
                 "ozon": "https://www.ozon.ru/product/{}/", "yandex": "https://market.yandex.ru/product/{}"}
    if market not in templates:
        raise MarketError("Неизвестный маркетплейс")
    return parse_link(templates[market].format(article))
