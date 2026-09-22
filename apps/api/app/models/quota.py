import enum
import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SqlEnum, TimestampMixin, UUIDPrimaryKeyMixin


class QuotaResourceClass(str, enum.Enum):
    CONCURRENT_JOB = "CONCURRENT_JOB"
    CONCURRENT_BUILD = "CONCURRENT_BUILD"
    DEPLOYED_SERVICE = "DEPLOYED_SERVICE"


class QuotaStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class QuotaReservation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "quota_reservations"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_class: Mapped[QuotaResourceClass] = mapped_column(
        SqlEnum(QuotaResourceClass, 50),
        nullable=False,
        index=True,
    )
    units: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[QuotaStatus] = mapped_column(
        SqlEnum(QuotaStatus, 50),
        nullable=False,
        default=QuotaStatus.ACTIVE,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

