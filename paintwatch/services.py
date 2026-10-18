import asyncio
import logging

from paintwatch.domain import CardRef, FetchError, Market, Snapshot, timestamp
from paintwatch.links import extract_urls, parse_link

logger = logging.getLogger(__name__)


class BatchService:
    def __init__(self, store, adapters, config):
        self.store, self.adapters, self.config = store, adapters, config

    async def collect(self, text: str, previous: list[dict]):
        urls, fragments = extract_urls(text)
        seen = {Snapshot.from_dict(s).ref.key for s in previous}
        snapshots, errors, skipped = (
            list(previous),
            [f"Не ссылка: {f}" for f in fragments],
            0,
        )
        for url in urls:
            if len(snapshots) >= self.config.max_batch:
                errors.append(
                    f"Лимит пакета: {self.config.max_batch}. Ссылка не добавлена: {url[:150]}"
                )
                continue
            try:
                ref = parse_link(url)
                if ref.key in seen or self.store.exists(ref):
                    skipped += 1
                    continue
                seen.add(ref.key)
                snap = await self.adapters[ref.market].fetch(ref)
                if snap.ref != ref or not snap.name:
                    raise FetchError("identity_mismatch", "Карточка не подтверждена")
                snapshots.append(snap.to_dict())
            except FetchError as e:
                errors.append(f"{url[:180]}: {e}")
            except Exception:
                logger.exception("Непредвиденная ошибка получения карточки")
                errors.append(
                    f"{url[:180]}: внутренняя ошибка адаптера; цена не принята"
                )
        if not urls and not fragments:
            errors.append("В сообщении нет URL")
        return snapshots, errors, skipped

    async def refresh(self, values):
        snapshots = []
        errors = []
        for value in values:
            snap = Snapshot.from_dict(value)
            if timestamp() - snap.observed_at > 120:
                try:
                    refreshed = await self.adapters[snap.ref.market].fetch(snap.ref)
                    if refreshed.ref != snap.ref:
                        raise FetchError("identity_mismatch", "Изменилась карточка")
                    # Сведения владельца отдельно от извлечённых характеристик.
                    for key, val in snap.characteristics.items():
                        if key.startswith("Владелец:"):
                            refreshed.characteristics[key] = val
                    snap = refreshed
                except FetchError as e:
                    snap.price = None
                    snap.issue = str(e)
                    errors.append(f"{snap.ref.article}: {e}")
            snapshots.append(snap)
        return snapshots, errors


class Monitor:
    def __init__(self, store, adapters, config):
        self.store, self.adapters, self.config = store, adapters, config
        self.lock = asyncio.Lock()
        self.running = False
        self.heartbeat = 0
        self.last_backup = 0

    async def check_due(self):
        if self.lock.locked():
            return None
        async with self.lock:
            checked = 0
            for p in self.store.due():
                ref = CardRef(Market(p["market"]), p["article"], p["url"], p["variant"])
                snapshot, error = None, None
                try:
                    snapshot = await self.adapters[p["market"]].fetch(ref)
                except FetchError as e:
                    error = e
                except Exception:
                    logger.exception(
                        "Непредвиденная ошибка адаптера, product_id=%s", p["id"]
                    )
                    error = FetchError(
                        "internal_error", "Внутренняя ошибка адаптера, смотрите журнал"
                    )
                self.store.apply(
                    p["id"], snapshot, error, self.config.interval(p["market"])
                )
                checked += 1
                self.heartbeat = timestamp()
            return checked

    async def run(self):
        self.running = True
        try:
            while True:
                self.heartbeat = timestamp()
                await self.check_due()
                if timestamp() - self.last_backup >= 86400:
                    destination = (
                        self.config.data_dir
                        / "backups"
                        / f"paintwatch-{timestamp()}.sqlite3"
                    )
                    self.store.backup(destination)
                    # Только собственные завершённые backup-файлы, сохраняем 14 последних.
                    for old in sorted(
                        destination.parent.glob("paintwatch-*.sqlite3"), reverse=True
                    )[14:]:
                        old.unlink()
                    self.last_backup = timestamp()
                await asyncio.sleep(5)
        finally:
            self.running = False
