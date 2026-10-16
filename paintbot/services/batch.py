from dataclasses import dataclass, field

from paintbot.marketplaces.links import MarketError, extract_links, resolve_link


@dataclass
class Batch:
    cards: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: int = 0
    processed: int = 0


async def collect(text, batch, adapters, store):
    links = extract_links(text)
    if not links:
        batch.errors.append("Ссылки http:// или https:// не найдены")
        return
    known = {(c.marketplace, c.card_id) for c in batch.cards}
    for url in links:
        if batch.processed >= 50:
            batch.errors.append("Пакет ограничен 50 ссылками. Остальные добавьте отдельным пакетом")
            break
        batch.processed += 1
        try:
            ref = await resolve_link(url, adapters)
            key = (ref.marketplace, ref.card_id)
            if key in known or store.exists(*key):
                batch.skipped += 1
                continue
            card = await adapters[ref.marketplace].fetch(ref)
            if (card.marketplace, card.card_id) != key:
                raise MarketError("Получена другая карточка")
            batch.cards.append(card)
            known.add(key)
        except MarketError as exc:
            batch.errors.append(f"{url[:120]} — {str(exc)[:350]}")
