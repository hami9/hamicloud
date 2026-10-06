import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional
import sqlalchemy as sa
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin, utc_now

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.workspace import Workspace


class DeadLetterRecord(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "dead_letter_records"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    last_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    job: Mapped["Job"] = relationship("Job")
    workspace: Mapped["Workspace"] = relationship("Workspace")

    __table_args__ = (
        sa.UniqueConstraint("job_id", name="uq_dead_letter_records_job_id"),
        sa.Index("ix_dead_letter_records_workspace_created", "workspace_id", "created_at", "id"),
    )
