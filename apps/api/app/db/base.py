import enum
from typing import Any, Optional, Type, TypeVar
import uuid
from datetime import datetime, timezone
import sqlalchemy as sa
from sqlalchemy import DateTime, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


E = TypeVar("E", bound=enum.Enum)


class SqlEnum(sa.types.TypeDecorator[E]):
    """String-backed Enum type decorator matching PostgreSQL VARCHAR(length) in Alembic without type drift."""

    impl = sa.String
    cache_ok = True

    def __init__(self, enum_cls: Type[E], length: int = 50, **kwargs: Any) -> None:
        super().__init__(length, **kwargs)
        self._enum_cls: Type[E] = enum_cls

    def process_bind_param(self, value: Any, dialect: sa.Dialect) -> Optional[str]:
        if value is None:
            return None
        return value.value if hasattr(value, "value") else str(value)

    def process_result_value(self, value: Any, dialect: sa.Dialect) -> Optional[E]:
        if value is None:
            return None
        return self._enum_cls(value)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, nullable=False
    )

