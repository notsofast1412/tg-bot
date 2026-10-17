import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit


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
         "ozon.ru": "ozon", "www.ozon.ru": "ozon"}
SUPPORTED_MARKETS = ("wb", "ozon")


def is_ozon_short(url):
    return bool(re.fullmatch(r"https?://(?:www\.)?ozon\.ru/t/[A-Za-z0-9]+/?(?:\?[^\s#]*)?(?:#[^\s]*)?", url))


async def resolve_link(url, adapters):
    try:
        return parse_link(url)
    except MarketError:
        if is_ozon_short(url):
            return await adapters["ozon"].resolve_short(url)
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
        raise MarketError("Нужна прямая ссылка WB или Ozon без логина и порта") from exc
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
    if not card_id:
        raise MarketError("Не распознана карточка. Нужна прямая ссылка WB/Ozon или Ozon /t/ без постороннего текста")
    return CardRef(market, card_id, canonical)


def from_article(market: str, article: str) -> CardRef:
    article = article.strip()
    if not re.fullmatch(r"[1-9]\d{0,19}", article):
        raise MarketError("WB и Ozon: числовой ID карточки. Для артикула продавца нет однозначного разрешения; добавьте прямую ссылку")
    templates = {"wb": "https://www.wildberries.ru/catalog/{}/detail.aspx",
                 "ozon": "https://www.ozon.ru/product/{}/"}
    if market not in templates:
        raise MarketError("Неизвестный маркетплейс")
    return parse_link(templates[market].format(article))
