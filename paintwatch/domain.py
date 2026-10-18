from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import re


def now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp() -> int:
    return int(now().timestamp())


class Market(StrEnum):
    WB = "wb"
    OZON = "ozon"


MARKET_NAMES = {"wb": "Wildberries", "ozon": "Ozon"}


class FetchError(Exception):
    def __init__(self, code: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class CardRef:
    market: Market
    article: str
    url: str
    variant: str = ""

    @property
    def key(self) -> str:
        return f"{self.market}:{self.article}:{self.variant}"


@dataclass
class Snapshot:
    ref: CardRef
    name: str
    price: int | None = None  # Всегда целое число копеек, валюта RUB.
    characteristics: dict = field(default_factory=dict)
    color: str | None = None
    volume: str | None = None
    pack_count: int | None = None
    source: str = ""
    price_kind: str = "обычная цена без персональных скидок"
    issue: str | None = None
    seller_id: str | None = None
    alternate_prices: dict[str, int] = field(default_factory=dict)
    observed_at: int = field(default_factory=timestamp)

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise FetchError("invalid_card", "Название карточки не подтверждено")
        if self.price is not None and (
            type(self.price) is not int or not 0 < self.price <= 10_000_000_000
        ):
            raise FetchError(
                "invalid_price",
                "Цена должна быть положительным целым числом копеек RUB",
            )

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        data = dict(data)
        ref = dict(data.pop("ref"))
        ref["market"] = Market(ref["market"])
        return cls(ref=CardRef(**ref), **data)


def kopecks(value) -> int:
    try:
        amount = Decimal(
            re.sub(r"\s+", "", str(value)).replace(",", ".").replace("₽", "")
        )
        cents = amount * 100
        if (
            not amount.is_finite()
            or amount <= 0
            or cents != cents.to_integral_value()
            or amount > 100_000_000
        ):
            raise ValueError
        return int(cents)
    except (InvalidOperation, ValueError, TypeError):
        raise FetchError("invalid_price", "Некорректная цена RUB") from None


def rub(value: int | None) -> str:
    if value is None:
        return "нет данных"
    return f"{Decimal(value) / 100:,.2f}".replace(",", " ") + " ₽"


DEFAULT_TAGS = {
    "#arton": 400,
    "#2arton": 750,
    "#6arton": 2200,
    "#belazo": 350,
    "#2belazo": 700,
    "#6belazo": 2000,
    "#trane": 350,
    "#3trane": 1000,
    "#6trane": 2000,
    "#flame": 500,
    "#molotow400": 5000,
    "#burner": 800,
    "#joggle": 300,
    "#2jiggle": 600,
    "#6jiggle": 1800,
    "#titans": 230,
}
