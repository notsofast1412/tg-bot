from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    threshold: Mapped[int] = mapped_column(Integer)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("market", "article", "variant"),
        Index("ix_products_due", "active", "next_check"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(12))
    article: Mapped[str] = mapped_column(String(100))
    variant: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    custom_name: Mapped[str] = mapped_column(String(200))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id"))
    threshold: Mapped[int] = mapped_column(Integer)
    custom_threshold: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_price: Mapped[int | None] = mapped_column(Integer)
    last_success: Mapped[int | None] = mapped_column(Integer)
    last_attempt: Mapped[int | None] = mapped_column(Integer)
    last_change: Mapped[int | None] = mapped_column(Integer)
    next_check: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(40), default="waiting")
    last_error: Mapped[str | None] = mapped_column(Text)
    failures: Mapped[int] = mapped_column(Integer, default=0)


class PriceHistory(Base):
    __tablename__ = "price_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    at: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(100))


class CheckLog(Base):
    __tablename__ = "check_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    at: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(100))


class NotificationState(Base):
    __tablename__ = "notification_state"
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    below: Mapped[bool] = mapped_column(Boolean, default=False)
    generation: Mapped[int] = mapped_column(Integer, default=0)


class Notification(Base):
    __tablename__ = "notification_queue"
    __table_args__ = (
        UniqueConstraint("product_id", "generation"),
        Index("ix_queue_due", "status", "next_attempt"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    generation: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[int] = mapped_column(Integer)
    sent_at: Mapped[int | None] = mapped_column(Integer)
    chat_id: Mapped[int | None] = mapped_column(Integer)
    message_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Draft(Base):
    __tablename__ = "drafts"
    owner_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    expires_at: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"
    update_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[int] = mapped_column(Integer)
