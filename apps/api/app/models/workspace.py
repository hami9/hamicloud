import enum
import uuid
from typing import TYPE_CHECKING, List
from sqlalchemy import ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SqlEnum, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.application import Application
    from app.models.job import Job


class WorkspaceRole(str, enum.Enum):
    OWNER = "OWNER"
    DEVELOPER = "DEVELOPER"
    VIEWER = "VIEWER"


class Workspace(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    memberships: Mapped[List["WorkspaceMembership"]] = relationship(
        "WorkspaceMembership", back_populates="workspace", cascade="all, delete-orphan"
    )
    applications: Mapped[List["Application"]] = relationship(
        "Application", back_populates="workspace", cascade="all, delete-orphan"
    )
    jobs: Mapped[List["Job"]] = relationship(
        "Job", back_populates="workspace", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("slug", name="uq_workspace_slug"),
    )


class WorkspaceMembership(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workspace_memberships"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[WorkspaceRole] = mapped_column(
        SqlEnum(WorkspaceRole, 50),
        nullable=False,
        default=WorkspaceRole.DEVELOPER,
    )

    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_subject", name="uq_workspace_membership_user"),
    )

