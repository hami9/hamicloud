import enum
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


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
        Enum(IntentResourceType, name="intent_resource_type_enum", native_enum=False),
        nullable=False,
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    target_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deterministic_resource_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[IntentStatus] = mapped_column(
        Enum(IntentStatus, name="intent_status_enum", native_enum=False),
        nullable=False,
        default=IntentStatus.PENDING,
        index=True,
    )
    claimed_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
