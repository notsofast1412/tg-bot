from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, delete, event, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import sessionmaker

from paintwatch.domain import DEFAULT_TAGS, CardRef, FetchError, Snapshot, timestamp
from .models import (
    CheckLog,
    Draft,
    Notification,
    NotificationState,
    PriceHistory,
    ProcessedUpdate,
    Product,
    Setting,
    Tag,
)


def record_dict(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class Store:
    def __init__(self, url: str):
        self.engine = create_engine(
            url, connect_args={"check_same_thread": False, "timeout": 15}
        )

        @event.listens_for(self.engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=15000")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self):
        with self.sessions() as session:
            # Резервируем SQLite writer до чтения состояния перехода.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def seed(self):
        with self.transaction() as session:
            for name, value in DEFAULT_TAGS.items():
                session.execute(
                    insert(Tag)
                    .values(name=name, threshold=value * 100)
                    .on_conflict_do_nothing(index_elements=["name"])
                )

    def setting(self, key: str, default=None):
        with self.sessions() as session:
            row = session.get(Setting, key)
            return row.value if row else default

    def set_setting(self, key: str, value):
        with self.transaction() as session:
            session.merge(Setting(key=key, value=str(value)))

    def tags(self) -> list[dict]:
        with self.sessions() as session:
            return [
                record_dict(t) for t in session.scalars(select(Tag).order_by(Tag.id))
            ]

    def update_tag(self, tag_id: int, threshold: int):
        if threshold <= 0:
            raise ValueError("Порог должен быть положительным")
        with self.transaction() as session:
            tag = session.get(Tag, tag_id)
            if not tag:
                raise ValueError("Тег не найден")
            tag.threshold = threshold
            for p in session.scalars(
                select(Product).where(
                    Product.tag_id == tag_id, Product.custom_threshold.is_(False)
                )
            ):
                p.threshold = threshold
                state = session.get(NotificationState, p.id)
                if state:
                    # Изменение настройки не является событием изменения цены.
                    state.below = p.last_price is not None and p.last_price < threshold
                session.execute(
                    update(Notification)
                    .where(
                        Notification.product_id == p.id,
                        Notification.status.in_(["pending", "blocked"]),
                    )
                    .values(status="cancelled", error="Изменён порог")
                )

    def exists(self, ref: CardRef) -> bool:
        with self.sessions() as session:
            return (
                session.scalar(
                    select(Product.id).where(
                        Product.market == ref.market,
                        Product.article == ref.article,
                        Product.variant == ref.variant,
                    )
                )
                is not None
            )

    def add_batch(
        self,
        snapshots: list[Snapshot],
        custom_name: str,
        tag_id: int,
        threshold: int | None,
        interval_by_market: dict[str, int],
    ):
        added, skipped = [], 0
        with self.transaction() as session:
            tag = session.get(Tag, tag_id)
            if not tag:
                raise ValueError("Тег не найден")
            for snapshot in snapshots:
                ref = snapshot.ref
                existing = session.scalar(
                    select(Product.id).where(
                        Product.market == ref.market,
                        Product.article == ref.article,
                        Product.variant == ref.variant,
                    )
                )
                if existing:
                    skipped += 1
                    continue
                p = Product(
                    market=ref.market,
                    article=ref.article,
                    variant=ref.variant,
                    url=ref.url,
                    name=snapshot.name,
                    custom_name=custom_name,
                    details=self.details(snapshot),
                    tag_id=tag_id,
                    threshold=threshold if threshold is not None else tag.threshold,
                    custom_threshold=threshold is not None,
                    next_check=timestamp(),
                    active=True,
                    status="waiting",
                    failures=0,
                )
                session.add(p)
                session.flush()
                session.add(
                    NotificationState(product_id=p.id, below=False, generation=0)
                )
                session.flush()
                self._apply(session, p, snapshot, None, interval_by_market[ref.market])
                added.append(p.id)
        return added, skipped

    @staticmethod
    def details(snapshot):
        return {
            "characteristics": snapshot.characteristics,
            "color": snapshot.color,
            "volume": snapshot.volume,
            "pack_count": snapshot.pack_count,
            "source": snapshot.source,
            "price_kind": snapshot.price_kind,
            "seller_id": snapshot.seller_id,
            "alternate_prices": snapshot.alternate_prices,
        }

    def apply(
        self,
        product_id: int,
        snapshot: Snapshot | None,
        error: FetchError | None,
        interval: int,
    ):
        with self.transaction() as session:
            p = session.get(Product, product_id)
            if p and p.active:
                if snapshot and (
                    snapshot.ref.market != p.market
                    or snapshot.ref.article != p.article
                    or snapshot.ref.variant != p.variant
                ):
                    error, snapshot = (
                        FetchError(
                            "identity_mismatch",
                            "Ответ относится к другой карточке/варианту",
                        ),
                        None,
                    )
                self._apply(session, p, snapshot, error, interval)

    def _apply(self, session, p, snapshot, error, interval):
        at = timestamp()
        p.last_attempt = at
        previous_seller = (p.details or {}).get("seller_id")
        if snapshot and previous_seller and snapshot.seller_id != previous_seller:
            error = FetchError(
                "seller_changed",
                "Продавец изменился или больше не подтверждён; цена не принята",
            )
        if (
            error
            or snapshot is None
            or snapshot.price is None
            or at - snapshot.observed_at > 120
            or snapshot.observed_at > at + 10
        ):
            p.failures += 1
            code = error.code if error else "no_price"
            reason = (
                str(error) if error else (snapshot.issue if snapshot else "Нет данных")
            )
            p.status, p.last_error = (
                code,
                reason or "Нет подтверждённой актуальной цены",
            )
            p.next_check = at + min(86400, interval * 2 ** min(p.failures - 1, 7))
            if error and error.retry_after:
                p.next_check = max(p.next_check, at + error.retry_after)
            session.add(
                CheckLog(
                    product_id=p.id,
                    at=at,
                    status=p.status,
                    error=p.last_error,
                    source=snapshot.source if snapshot else None,
                )
            )
            return
        previous = p.last_price
        details = self.details(snapshot)
        for key, value in (p.details or {}).get("characteristics", {}).items():
            if key.startswith("Владелец:"):
                details["characteristics"][key] = value
        p.name, p.details = snapshot.name, details
        p.last_price, p.last_success = snapshot.price, snapshot.observed_at
        p.status, p.last_error, p.failures = "ok", None, 0
        p.next_check = at + interval
        session.add(
            CheckLog(product_id=p.id, at=at, status="ok", source=snapshot.source)
        )
        if previous != snapshot.price:
            p.last_change = snapshot.observed_at
            session.add(
                PriceHistory(
                    product_id=p.id,
                    at=snapshot.observed_at,
                    price=snapshot.price,
                    source=snapshot.source,
                )
            )
        state = session.get(NotificationState, p.id)
        below = snapshot.price < p.threshold
        if below and not state.below:
            state.generation += 1
            tag = session.get(Tag, p.tag_id)
            session.add(
                Notification(
                    product_id=p.id,
                    generation=state.generation,
                    created_at=at,
                    payload={
                        "name": p.custom_name,
                        "card_name": p.name,
                        "market": p.market,
                        "article": p.article,
                        "url": p.url,
                        "price": snapshot.price,
                        "previous": previous,
                        "threshold": p.threshold,
                        "tag": tag.name,
                        "observed_at": snapshot.observed_at,
                        "price_kind": snapshot.price_kind,
                    },
                )
            )
        state.below = below

    def get_product(self, product_id: int) -> dict | None:
        with self.sessions() as session:
            p = session.get(Product, product_id)
            return self._product_dict(session, p) if p else None

    @staticmethod
    def _product_dict(session, p):
        d = record_dict(p)
        d["tag"] = session.get(Tag, p.tag_id).name
        return d

    def products(self, page=0, market=None, tag_id=None, status=None, limit=5):
        with self.sessions() as session:
            statement = select(Product)
            if market:
                statement = statement.where(Product.market == market)
            if tag_id:
                statement = statement.where(Product.tag_id == tag_id)
            if status == "error":
                statement = statement.where(
                    Product.status != "ok", Product.active.is_(True)
                )
            elif status == "paused":
                statement = statement.where(Product.active.is_(False))
            elif status == "active":
                statement = statement.where(Product.active.is_(True))
            count = session.scalar(
                select(func.count()).select_from(statement.subquery())
            )
            return [
                self._product_dict(session, p)
                for p in session.scalars(
                    statement.order_by(Product.id).offset(page * limit).limit(limit)
                )
            ], count

    def toggle(self, product_id: int):
        with self.transaction() as session:
            p = session.get(Product, product_id)
            if p:
                p.active = not p.active
                if not p.active:
                    session.execute(
                        update(Notification)
                        .where(
                            Notification.product_id == p.id,
                            Notification.status.in_(["pending", "blocked"]),
                        )
                        .values(status="cancelled", error="Товар приостановлен")
                    )

    def update_product_threshold(self, product_id: int, threshold: int):
        with self.transaction() as session:
            p = session.get(Product, product_id)
            if not p or threshold <= 0:
                raise ValueError("Товар не найден или порог некорректен")
            p.threshold, p.custom_threshold = threshold, True
            session.get(NotificationState, p.id).below = (
                p.last_price is not None and p.last_price < threshold
            )
            session.execute(
                update(Notification)
                .where(
                    Notification.product_id == p.id,
                    Notification.status.in_(["pending", "blocked"]),
                )
                .values(status="cancelled", error="Изменён порог товара")
            )

    def set_group(self, group_id):
        with self.transaction() as session:
            session.merge(Setting(key="group_id", value=str(group_id)))
            session.execute(
                update(Notification)
                .where(Notification.status == "blocked")
                .values(status="pending", next_attempt=0)
            )

    def due(self, limit=20):
        with self.sessions() as session:
            return [
                record_dict(p)
                for p in session.scalars(
                    select(Product)
                    .where(Product.active.is_(True), Product.next_check <= timestamp())
                    .order_by(Product.next_check)
                    .limit(limit)
                )
            ]

    def history(self, product_id: int):
        with self.sessions() as session:
            prices = [
                record_dict(p)
                for p in session.scalars(
                    select(PriceHistory)
                    .where(PriceHistory.product_id == product_id)
                    .order_by(PriceHistory.id.desc())
                    .limit(20)
                )
            ]
            errors = [
                record_dict(p)
                for p in session.scalars(
                    select(CheckLog)
                    .where(CheckLog.product_id == product_id, CheckLog.status != "ok")
                    .order_by(CheckLog.id.desc())
                    .limit(10)
                )
            ]
            return prices, errors

    def statistics(self):
        with self.sessions() as session:
            return {
                "active": session.scalar(
                    select(func.count())
                    .select_from(Product)
                    .where(Product.active.is_(True))
                ),
                "errors": session.scalar(
                    select(func.count())
                    .select_from(Product)
                    .where(Product.active.is_(True), Product.status != "ok")
                ),
                "last_success": session.scalar(select(func.max(Product.last_success))),
                "last_attempt": session.scalar(select(func.max(Product.last_attempt))),
                "queue": session.scalar(
                    select(func.count())
                    .select_from(Notification)
                    .where(
                        Notification.status.in_(
                            ["pending", "blocked", "uncertain", "sending"]
                        )
                    )
                ),
                "uncertain": session.scalar(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.status == "uncertain")
                ),
                "markets": {
                    m: dict(
                        session.execute(
                            select(Product.status, func.count())
                            .where(Product.market == m, Product.active.is_(True))
                            .group_by(Product.status)
                        ).all()
                    )
                    for m in ("wb", "ozon")
                },
            }

    def save_draft(self, owner_id: int, data: dict, ttl: int):
        with self.transaction() as session:
            session.merge(
                Draft(owner_id=owner_id, data=data, expires_at=timestamp() + ttl)
            )

    def draft(self, owner_id: int):
        with self.transaction() as session:
            draft = session.get(Draft, owner_id)
            if draft and draft.expires_at <= timestamp():
                session.delete(draft)
                return {"expired": True}
            return draft.data if draft else None

    def clear_draft(self, owner_id: int):
        with self.transaction() as session:
            session.execute(delete(Draft).where(Draft.owner_id == owner_id))

    def processed(self, update_id):
        with self.sessions() as session:
            return session.get(ProcessedUpdate, update_id) is not None

    def mark_processed(self, update_id):
        with self.transaction() as session:
            session.execute(
                insert(ProcessedUpdate)
                .values(update_id=update_id, at=timestamp())
                .on_conflict_do_nothing()
            )
            session.execute(
                delete(ProcessedUpdate).where(
                    ProcessedUpdate.at < timestamp() - 7 * 86400
                )
            )

    def recover_queue(self):
        with self.transaction() as session:
            session.execute(
                update(Notification)
                .where(Notification.status == "sending")
                .values(
                    status="uncertain",
                    error="Перезапуск во время отправки: доставка неизвестна",
                )
            )

    def claim_notification(self):
        with self.transaction() as session:
            n = session.scalar(
                select(Notification)
                .where(
                    Notification.status.in_(["pending", "blocked"]),
                    Notification.next_attempt <= timestamp(),
                )
                .order_by(Notification.id)
                .limit(1)
            )
            if not n:
                return None
            group = session.get(Setting, "group_id")
            if not group:
                return None
            product = session.get(Product, n.product_id)
            state = session.get(NotificationState, n.product_id)
            if (
                not product.active
                or not state.below
                or state.generation != n.generation
            ):
                n.status, n.error = (
                    "cancelled",
                    "Условие уже не действует или заменено новым переходом",
                )
                return None
            if (
                product.status != "ok"
                or product.last_success is None
                or product.next_check < timestamp()
            ):
                n.next_attempt = timestamp() + 60
                n.error = "Ожидание свежей достоверной проверки цены"
                return None
            # После задержки отправляем последнюю подтверждённую цену того же перехода.
            n.payload = {
                **n.payload,
                "price": product.last_price,
                "observed_at": product.last_success,
            }
            n.status, n.attempts = "sending", n.attempts + 1
            n.chat_id = int(group.value)
            return record_dict(n)

    def finish_notification(
        self, notification_id, status, error=None, delay=0, message_id=None
    ):
        with self.transaction() as session:
            n = session.get(Notification, notification_id)
            n.status, n.error = status, error
            n.next_attempt = timestamp() + delay
            n.message_id = message_id
            if status == "sent":
                n.sent_at = timestamp()

    def retry_uncertain(self, notification_id):
        with self.transaction() as session:
            n = session.get(Notification, notification_id)
            if not n or n.status != "uncertain":
                return False
            n.status, n.next_attempt = "pending", timestamp()
            return True

    def uncertain_notifications(self):
        with self.sessions() as session:
            return [
                record_dict(n)
                for n in session.scalars(
                    select(Notification)
                    .where(Notification.status == "uncertain")
                    .limit(20)
                )
            ]

    def backup(self, destination: Path):
        import sqlite3

        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.engine.connect() as source, sqlite3.connect(destination) as target:
            source.connection.driver_connection.backup(target)
        destination.chmod(0o600)
