import enum
import uuid
from typing import TYPE_CHECKING, List, Optional
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SqlEnum, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.release import Release
    from app.models.workspace import Workspace


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
        SqlEnum(WorkloadType, 50),
        nullable=False,
        default=WorkloadType.HTTP_SERVICE,
    )
    desired_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_release_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("releases.id", ondelete="SET NULL", use_alter=True), nullable=True
    )

    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="applications")
    releases: Mapped[List["Release"]] = relationship(
        "Release",
        back_populates="application",
        cascade="all, delete-orphan",
        foreign_keys="Release.application_id",
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_application_workspace_slug"),
    )

