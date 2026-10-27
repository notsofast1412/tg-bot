from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import parse_qs, urlencode, urlsplit


class PriceError(Exception):
    def __init__(self, message, *, code="unavailable", retry_after=0):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class ProductRef:
    market: str
    article: str
    url: str
    variant: str = ""


@dataclass
class Snapshot:
    ref: ProductRef
    name: str
    price: int | None
    seller: str = ""
    color: str = ""
    volume: str = ""
    pack: str = ""
    issue: str = ""
    source: str = ""


TAGS = [
    ("#arton", 40000),
    ("#2arton", 75000),
    ("#6arton", 220000),
    ("#belazo", 35000),
    ("#2belazo", 70000),
    ("#6belazo", 200000),
    ("#trane", 35000),
    ("#3trane", 100000),
    ("#6trane", 200000),
    ("#flame", 50000),
    ("#molotow400", 500000),
    ("#burner", 80000),
    ("#joggle", 30000),
    ("#2jiggle", 60000),
    ("#6jiggle", 180000),
    ("#titans", 23000),
]


def money(value):
    """Parse roubles; never round or silently accept missing/zero prices."""
    try:
        value = re.sub(r"\s+", "", str(value)).replace("₽", "").replace(",", ".")
        result = Decimal(value) * 100
        if (
            not result.is_finite()
            or result != result.to_integral_value()
            or not 0 < result <= 10**10
        ):
            raise ValueError
        return int(result)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(
            "Укажите положительную цену в рублях, не более двух знаков после запятой"
        ) from None


def rub(value):
    return "—" if value is None else f"{value / 100:,.2f} ₽".replace(",", " ")


def display_time(value):
    return (
        datetime.fromtimestamp(value, timezone(timedelta(hours=3))).strftime(
            "%d.%m.%Y %H:%M МСК"
        )
        if value
        else "—"
    )


def parse_ref(text, market=""):
    text = text.strip()
    if text.isdigit():
        if market not in ("wb", "ozon"):
            raise ValueError("Для числового артикула выберите площадку")
        text = (
            f"https://www.ozon.ru/product/{text}/"
            if market == "ozon"
            else f"https://www.wildberries.ru/catalog/{text}/detail.aspx"
        )
    try:
        p = urlsplit(text)
        if p.scheme not in ("https", "http") or p.username or p.password or p.port:
            raise ValueError
        if p.hostname in ("ozon.ru", "www.ozon.ru"):
            match = re.fullmatch(r"/product/(?:[^/]+-)?(\d+)/?", p.path)
            if match:
                return ProductRef(
                    "ozon", match[1], f"https://www.ozon.ru/product/{match[1]}/"
                )
        if p.hostname in ("wildberries.ru", "www.wildberries.ru"):
            match = re.fullmatch(r"/catalog/(\d+)/detail\.aspx/?", p.path)
            variant = parse_qs(p.query).get("size", [""])[0]
            if match and (not variant or variant.isdigit()):
                url = f"https://www.wildberries.ru/catalog/{match[1]}/detail.aspx"
                if variant:
                    url += "?" + urlencode({"size": variant})
                return ProductRef("wb", match[1], url, variant)
    except ValueError:
        pass
    raise ValueError(
        "Нужна прямая ссылка на карточку Wildberries/Ozon или числовой артикул"
    )


def parse_batch(text, market=""):
    parts = re.findall(r"https?://(?:(?!https?://)[^\s<>\"'`])+|(?<!\S)\d+(?!\S)", text)
    residue = re.sub(
        r"https?://(?:(?!https?://)[^\s<>\"'`])+|(?<!\S)\d+(?!\S)", "", text
    ).strip(" \t\n,;")
    if not parts or residue or len(parts) > 1000:
        raise ValueError(
            "Вставьте до 1000 прямых ссылок или числовых артикулов, по одному на строку"
        )
    unique = {}
    for part in parts:
        ref = parse_ref(part.rstrip(".,;!)]}"), market)
        unique[(ref.market, ref.article, ref.variant)] = ref
    return list(unique.values())
