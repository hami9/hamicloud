import enum
import uuid
from typing import List, Optional
from sqlalchemy import Enum, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WorkloadType(str, enum.Enum):
    HTTP_SERVICE = "HTTP_SERVICE"


class Application(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "applications"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    workload_type: Mapped[WorkloadType] = mapped_column(
        Enum(WorkloadType, name="workload_type_enum", native_enum=False),
        nullable=False,
        default=WorkloadType.HTTP_SERVICE,
    )
    desired_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_release_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, nullable=True
    )

    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="applications")  # type: ignore[name-defined]
    releases: Mapped[List["Release"]] = relationship(  # type: ignore[name-defined]
        "Release", back_populates="application", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_application_workspace_slug"),
    )
