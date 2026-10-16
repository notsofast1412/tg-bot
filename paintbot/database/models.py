from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    threshold: Mapped[int] = mapped_column(Integer)  # kopecks throughout


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("marketplace", "card_id"), Index("ix_products_due", "active", "next_check"))
    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace: Mapped[str] = mapped_column(String)
    card_id: Mapped[str] = mapped_column(String)
    article: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    custom_name: Mapped[str] = mapped_column(Text)
    attributes: Mapped[str] = mapped_column(Text, default="{}")
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"))
    last_price: Mapped[int | None] = mapped_column(Integer)
    last_success: Mapped[int | None] = mapped_column(Integer)
    last_attempt: Mapped[int | None] = mapped_column(Integer)
    last_changed: Mapped[int | None] = mapped_column(Integer)
    next_check: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(Text, default="Ожидание первой проверки")
    failures: Mapped[int] = mapped_column(Integer, default=0)


class PriceHistory(Base):
    __tablename__ = "price_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    price: Mapped[int] = mapped_column(Integer)
    checked_at: Mapped[int] = mapped_column(Integer)


class CheckLog(Base):
    __tablename__ = "check_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    checked_at: Mapped[int] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(Text)


class NotificationState(Base):
    __tablename__ = "notification_state"
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    below: Mapped[bool] = mapped_column(Boolean, default=False)
    generation: Mapped[int] = mapped_column(Integer, default=0)


class NotificationQueue(Base):
    __tablename__ = "notification_queue"
    __table_args__ = (UniqueConstraint("product_id", "generation"), Index("ix_queue_due", "state", "next_attempt"))
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    generation: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[int] = mapped_column(Integer, default=0)
    message_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"
    update_id: Mapped[int] = mapped_column(primary_key=True)
    processed_at: Mapped[int] = mapped_column(Integer)
