import enum
import uuid
from typing import TYPE_CHECKING, Any, Dict, Optional
from sqlalchemy import JSON, Enum, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.application import Application


class ReleaseStatus(str, enum.Enum):
    REQUESTED = "REQUESTED"
    BUILDING = "BUILDING"
    IMAGE_READY = "IMAGE_READY"
    DEPLOYING = "DEPLOYING"
    HEALTHY = "HEALTHY"
    BUILD_FAILED = "BUILD_FAILED"
    DEPLOY_FAILED = "DEPLOY_FAILED"


class Release(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "releases"

    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    release_number: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    config_json: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[ReleaseStatus] = mapped_column(
        Enum(ReleaseStatus, name="release_status_enum", native_enum=False),
        nullable=False,
        default=ReleaseStatus.REQUESTED,
        index=True,
    )
    status_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    application: Mapped["Application"] = relationship("Application", back_populates="releases")

    __table_args__ = (
        UniqueConstraint("application_id", "release_number", name="uq_release_app_number"),
    )
