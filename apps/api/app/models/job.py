import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional
from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class JobState(str, enum.Enum):
    QUEUED = "QUEUED"
    ADMITTED = "ADMITTED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class Job(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "jobs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    command_args: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    env_vars: Mapped[Dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    current_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, name="job_state_enum", native_enum=False),
        nullable=False,
        default=JobState.QUEUED,
        index=True,
    )

    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="jobs")
    attempts: Mapped[List["JobAttempt"]] = relationship(
        "JobAttempt", back_populates="job", cascade="all, delete-orphan", order_by="JobAttempt.attempt_number"
    )


class JobAttempt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "job_attempts"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, name="job_attempt_state_enum", native_enum=False),
        nullable=False,
        default=JobState.QUEUED,
        index=True,
    )
    resource_uid: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped["Job"] = relationship("Job", back_populates="attempts")

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt_number"),
    )
