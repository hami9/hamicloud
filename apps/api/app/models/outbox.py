import enum
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from sqlalchemy import JSON, DateTime, Enum, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class OutboxStatus(str, enum.Enum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class OutboxEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "outbox_events"

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, unique=True, nullable=False, default=uuid.uuid4, index=True
    )
    topic: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
    headers_json: Mapped[Dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status_enum", native_enum=False),
        nullable=False,
        default=OutboxStatus.PENDING,
        index=True,
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ConsumedEvent(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "consumed_events"

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    consumer_group: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("event_id", "consumer_group", name="uq_consumed_event_group"),
    )
