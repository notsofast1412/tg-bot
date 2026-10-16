from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
from zoneinfo import ZoneInfo


def money(value: int | None) -> str:
    if value is None:
        return "не получена"
    return f"{Decimal(value) / 100:,.2f}".replace(",", " ") + " ₽"


def kopecks(value: str | int | float) -> int:
    try:
        parsed = Decimal(str(value).replace(" ", "").replace(",", "."))
        if not parsed.is_finite() or parsed <= 0 or parsed > 100_000_000:
            raise ValueError("Цена должна быть положительной и не больше 100 млн ₽")
        if parsed * 100 != (parsed * 100).to_integral_value():
            raise ValueError("Не более двух знаков после запятой")
        return int(parsed * 100)
    except InvalidOperation as exc:
        raise ValueError("Некорректная цена") from exc


def stamp(value: int | None) -> str:
    return datetime.fromtimestamp(value, ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M МСК") if value else "не было"


def alert(product, tag, price, previous):
    delta = f"\n📉 Изменение: {money(price - previous)}" if previous is not None else ""
    return (f"🔻 <b>Цена ниже порога!</b>\n\n🎨 {escape(product.custom_name)}\n"
            f"{escape(product.title)}\n🏪 Маркетплейс: {escape(product.marketplace)}\n"
            f"🔢 Артикул: {escape(product.article)}\n💰 Текущая цена: {money(price)}\n"
            f"🎯 Порог: {money(tag.threshold)}{delta}\n🏷 Тег: {escape(tag.name)}\n"
            f'<a href="{escape(product.url, quote=True)}">🔗 Открыть товар</a>')
