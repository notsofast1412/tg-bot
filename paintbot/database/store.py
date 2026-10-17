import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.orm import Session

from .models import (CheckLog, NotificationQueue, NotificationState, PriceHistory,
                     ProcessedUpdate, Product, Setting, Tag)
from paintbot.services.formatting import alert
from paintbot.marketplaces.links import SUPPORTED_MARKETS

TAGS = {
    "#arton": 400, "#2arton": 750, "#6arton": 2200,
    "#belazo": 350, "#2belazo": 700, "#6belazo": 2000,
    "#trane": 350, "#3trane": 1000, "#6trane": 2000,
    "#flame": 500, "#molotow400": 5000, "#burner": 800,
    "#joggle": 300, "#2jiggle": 600, "#6jiggle": 1800, "#titans": 230,
}


class Store:
    def __init__(self, path: str | Path):
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.path = path
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 30})

        @event.listens_for(self.engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")

    @contextmanager
    def tx(self):
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            yield session

    def seed(self):
        with self.tx() as s:
            existing = set(s.scalars(select(Tag.name)))
            s.add_all(Tag(name=name, threshold=value * 100) for name, value in TAGS.items() if name not in existing)
            # Keep legacy products and their history, but stop removed marketplaces.
            unsupported = select(Product.id).where(Product.marketplace.not_in(SUPPORTED_MARKETS))
            s.execute(update(Product).where(Product.marketplace.not_in(SUPPORTED_MARKETS)).values(
                active=False, status="Поддержка площадки прекращена"))
            s.execute(update(NotificationQueue).where(NotificationQueue.product_id.in_(unsupported),
                NotificationQueue.state == "pending").values(
                state="cancelled", error="Поддержка площадки прекращена"))

    def recover(self):
        with self.tx() as s:
            s.execute(update(NotificationQueue).where(NotificationQueue.state == "sending").values(
                state="uncertain", error="Перезапуск во время отправки: доставка неизвестна"))
            for p in s.scalars(select(Product).where(Product.status == "Проверка выполняется")):
                p.status = "Предыдущая проверка прервана перезапуском"
                p.failures += 1
                s.add(CheckLog(product_id=p.id, checked_at=int(time.time()), success=False, detail=p.status))

    def setting(self, key, value=None):
        with self.tx() as s:
            row = s.get(Setting, key)
            if value is not None:
                if row:
                    row.value = str(value)
                else:
                    row = Setting(key=key, value=str(value))
                    s.add(row)
            return row.value if row else None

    def seen_update(self, update_id):
        with self.tx() as s:
            if s.get(ProcessedUpdate, update_id):
                return True
            s.add(ProcessedUpdate(update_id=update_id, processed_at=int(time.time())))
            # Telegram update ids are retained for a week; bounded growth.
            s.query(ProcessedUpdate).filter(ProcessedUpdate.processed_at < int(time.time()) - 604800).delete()
            return False

    def tags(self):
        with self.tx() as s:
            return list(s.scalars(select(Tag).order_by(Tag.id)))

    def edit_threshold(self, name, threshold):
        if threshold <= 0:
            raise ValueError("Порог должен быть положительным")
        with self.tx() as s:
            tag = s.scalar(select(Tag).where(Tag.name == name))
            if not tag:
                raise ValueError("Неизвестный тег")
            tag.threshold = threshold
            for p in s.scalars(select(Product).where(Product.tag_id == tag.id)):
                state = s.get(NotificationState, p.id)
                # A settings change establishes a baseline, never an artificial price event.
                if p.last_price is not None:
                    state.below = p.last_price < threshold
                s.execute(update(NotificationQueue).where(
                    NotificationQueue.product_id == p.id,
                    NotificationQueue.state == "pending").values(state="cancelled", error="Изменён порог"))

    def exists(self, market, card_id):
        with self.tx() as s:
            return s.scalar(select(Product.id).where(Product.marketplace == market, Product.card_id == card_id)) is not None

    def add_batch(self, cards, name, tag_id):
        added, skipped = [], 0
        with self.tx() as s:
            if not s.get(Tag, tag_id):
                raise ValueError("Тег не найден")
            for card in cards:
                if card.marketplace not in SUPPORTED_MARKETS:
                    raise ValueError("Поддерживаются только WB и Ozon")
                if s.scalar(select(Product.id).where(Product.marketplace == card.marketplace, Product.card_id == card.card_id)):
                    skipped += 1
                    continue
                p = Product(marketplace=card.marketplace, card_id=card.card_id, article=card.article,
                            url=card.url, title=card.title, custom_name=name,
                            attributes=json.dumps(card.attributes, ensure_ascii=False), tag_id=tag_id)
                s.add(p)
                s.flush()
                s.add(NotificationState(product_id=p.id, below=False, generation=0))
                added.append(p.id)
            # Preview prices are intentionally not reused as a new monitoring observation.
        return added, skipped

    def products(self, market=None, tag=None, status=None, offset=0, limit=5):
        with self.tx() as s:
            q = select(Product, Tag).join(Tag)
            if market:
                q = q.where(Product.marketplace == market)
            if tag:
                q = q.where(Tag.name == tag)
            if status == "error":
                q = q.where(Product.failures > 0)
            elif status == "active":
                q = q.where(Product.active.is_(True))
            elif status == "paused":
                q = q.where(Product.active.is_(False))
            count = s.scalar(select(func.count()).select_from(q.subquery()))
            return list(s.execute(q.order_by(Product.id).offset(offset).limit(limit))), count

    def product(self, product_id):
        with self.tx() as s:
            return s.get(Product, product_id)

    def set_active(self, product_id, active):
        with self.tx() as s:
            p = s.get(Product, product_id)
            if not p:
                raise ValueError("Товар не найден")
            if active and p.marketplace not in SUPPORTED_MARKETS:
                raise ValueError("Поддержка площадки прекращена")
            p.active = active
            if not active:
                s.execute(update(NotificationQueue).where(NotificationQueue.product_id == p.id,
                    NotificationQueue.state == "pending").values(state="cancelled", error="Мониторинг приостановлен"))

    def due(self, now, limit=20):
        with self.tx() as s:
            return list(s.scalars(select(Product).where(Product.active.is_(True), Product.next_check <= now)
                                  .where(Product.marketplace.in_(SUPPORTED_MARKETS))
                                  .order_by(Product.next_check, Product.id).limit(limit)))

    def begin_attempt(self, product_id, interval):
        with self.tx() as s:
            p = s.get(Product, product_id)
            if p and p.active:
                p.last_attempt = int(time.time())
                p.status = "Проверка выполняется"
                p.next_check = p.last_attempt + interval

    def record(self, product_id, card, error, interval, now=None):
        now = int(time.time()) if now is None else now
        with self.tx() as s:
            p = s.get(Product, product_id)
            if not p or not p.active:
                return
            if p.status != "Проверка выполняется":
                p.last_attempt = now
            if card is not None and (card.card_id != p.card_id or card.marketplace != p.marketplace):
                error = "Идентификатор полученной карточки не совпадает"
            price = card.price if card and not error else None
            if price is not None and (not isinstance(price, int) or isinstance(price, bool) or price <= 0):
                error, price = "Некорректная цена адаптера", None
            if price is None:
                p.failures += 1
                p.status = error or (card.warning if card else None) or "Цена не получена"
                p.next_check = now + min(interval * 2 ** min(p.failures - 1, 5), max(interval, 21600))
                s.add(CheckLog(product_id=p.id, checked_at=now, success=False, detail=p.status))
                return
            previous = p.last_price
            p.status, p.failures = "OK", 0
            p.last_success, p.next_check = now, now + interval
            p.last_price = price
            if previous != price:
                p.last_changed = now
                s.add(PriceHistory(product_id=p.id, price=price, checked_at=now))
            s.add(CheckLog(product_id=p.id, checked_at=now, success=True, detail=card.source))
            tag = s.get(Tag, p.tag_id)
            state = s.get(NotificationState, p.id)
            below = price < tag.threshold
            if below and not state.below:
                state.generation += 1
                s.add(NotificationQueue(product_id=p.id, generation=state.generation,
                                        text=alert(p, tag, price, previous)))
            state.below = below

    def history(self, product_id, limit=20):
        with self.tx() as s:
            changes = list(s.scalars(select(PriceHistory).where(PriceHistory.product_id == product_id)
                           .order_by(PriceHistory.id.desc()).limit(limit)))
            errors = list(s.scalars(select(CheckLog).where(CheckLog.product_id == product_id,
                          CheckLog.success.is_(False)).order_by(CheckLog.id.desc()).limit(limit)))
            return changes, errors

    def claim_notification(self, now):
        with self.tx() as s:
            q = s.scalar(select(NotificationQueue).where(NotificationQueue.state == "pending",
                         NotificationQueue.product_id.in_(select(Product.id).where(
                             Product.marketplace.in_(SUPPORTED_MARKETS))),
                         NotificationQueue.next_attempt <= now).order_by(NotificationQueue.id).limit(1))
            if q:
                q.state = "sending"
                q.attempts += 1
            return q

    def finish_notification(self, queue_id, state, error=None, delay=0, message_id=None):
        with self.tx() as s:
            row = s.get(NotificationQueue, queue_id)
            row.state, row.error = state, error
            row.next_attempt = int(time.time()) + delay
            row.message_id = message_id

    def queue(self):
        with self.tx() as s:
            return list(s.scalars(select(NotificationQueue).where(
                NotificationQueue.state.in_(["pending", "uncertain", "failed"]))
                .order_by(NotificationQueue.id).limit(30)))

    def retry_notification(self, queue_id):
        with self.tx() as s:
            q = s.get(NotificationQueue, queue_id)
            if not q or q.state not in ("uncertain", "failed"):
                raise ValueError("Нет такого проблемного уведомления")
            if s.get(Product, q.product_id).marketplace not in SUPPORTED_MARKETS:
                raise ValueError("Поддержка площадки прекращена")
            q.state, q.next_attempt = "pending", 0

    def statistics(self):
        with self.tx() as s:
            total = s.scalar(select(func.count()).select_from(Product).where(Product.active.is_(True)))
            errors = s.scalar(select(func.count()).select_from(Product).where(Product.failures > 0))
            queue = s.scalar(select(func.count()).select_from(NotificationQueue).where(
                NotificationQueue.state.in_(["pending", "sending", "uncertain", "failed"])))
            markets = {}
            for market in SUPPORTED_MARKETS:
                p = s.scalar(select(Product).where(Product.marketplace == market,
                             Product.last_attempt.is_not(None)).order_by(Product.last_attempt.desc()).limit(1))
                markets[market] = f"{p.status}" if p else "не проверялся"
            return dict(active=total, errors=errors, queue=queue, markets=markets,
                        last_success=s.scalar(select(func.max(Product.last_success))),
                        last_attempt=s.scalar(select(func.max(Product.last_attempt))))
