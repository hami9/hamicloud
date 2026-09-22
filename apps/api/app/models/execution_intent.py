import enum
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SqlEnum, TimestampMixin, UUIDPrimaryKeyMixin


class IntentStatus(str, enum.Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    APPLIED = "APPLIED"
    TERMINATED = "TERMINATED"


class IntentResourceType(str, enum.Enum):
    JOB_ATTEMPT = "JOB_ATTEMPT"
    SERVICE_RELEASE = "SERVICE_RELEASE"


class ExecutionIntent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "execution_intents"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_type: Mapped[IntentResourceType] = mapped_column(
        SqlEnum(IntentResourceType, 50),
        nullable=False,
    )
    job_attempt_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("job_attempts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    release_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("releases.id", ondelete="CASCADE"), nullable=True, index=True
    )
    resource_uid: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    target_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deterministic_resource_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[IntentStatus] = mapped_column(
        SqlEnum(IntentStatus, 50),
        nullable=False,
        default=IntentStatus.PENDING,
        index=True,
    )
    claimed_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "(resource_type = 'JOB_ATTEMPT' AND job_attempt_id IS NOT NULL AND release_id IS NULL) OR "
            "(resource_type = 'SERVICE_RELEASE' AND release_id IS NOT NULL AND job_attempt_id IS NULL)",
            name="ck_execution_intents_typed_resource",
        ),
        UniqueConstraint(
            "resource_type",
            "job_attempt_id",
            "release_id",
            "target_generation",
            name="uq_execution_intents_target",
            postgresql_nulls_not_distinct=True,
        ),
    )

