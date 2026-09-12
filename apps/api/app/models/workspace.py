import enum
import uuid
from typing import List
from sqlalchemy import Enum, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WorkspaceRole(str, enum.Enum):
    OWNER = "owner"
    DEVELOPER = "developer"
    VIEWER = "viewer"


class Workspace(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)

    memberships: Mapped[List["WorkspaceMembership"]] = relationship(
        "WorkspaceMembership", back_populates="workspace", cascade="all, delete-orphan"
    )
    applications: Mapped[List["Application"]] = relationship(  # type: ignore[name-defined]
        "Application", back_populates="workspace", cascade="all, delete-orphan"
    )
    jobs: Mapped[List["Job"]] = relationship(  # type: ignore[name-defined]
        "Job", back_populates="workspace", cascade="all, delete-orphan"
    )


class WorkspaceMembership(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workspace_memberships"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[WorkspaceRole] = mapped_column(
        Enum(WorkspaceRole, name="workspace_role_enum", native_enum=False),
        nullable=False,
        default=WorkspaceRole.DEVELOPER,
    )

    workspace: Mapped["Workspace"] = relationship("Workspace", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_subject", name="uq_workspace_membership_user"),
    )
