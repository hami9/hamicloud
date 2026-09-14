from dataclasses import dataclass
from typing import Optional
import uuid
from fastapi import Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.workspace import WorkspaceMembership, WorkspaceRole


@dataclass(frozen=True)
class Caller:
    """Authenticated caller identity."""
    subject: str


async def get_caller(
    x_actor_subject: Optional[str] = Header(None, alias="X-Actor-Subject"),
    x_dev_subject: Optional[str] = Header(None, alias="X-Dev-Subject"),
) -> Caller:
    """Establish caller identity.

    DEVELOPMENT SEAM (Decision D1):
    In development (settings.ENVIRONMENT == 'development'), reads caller subject
    from X-Actor-Subject (or X-Dev-Subject) header. In any other environment, or if
    the header is absent or empty, raises HTTP 401 Unauthorized until OIDC bearer
    tokens arrive in M1.

    NOTE: This is a development seam, NOT a security control.
    """
    actor = x_actor_subject.strip() if x_actor_subject else None
    dev = x_dev_subject.strip() if x_dev_subject else None
    subject = actor or dev
    if settings.ENVIRONMENT == "development" and subject:
        return Caller(subject=subject)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


ROLE_RANK = {
    WorkspaceRole.VIEWER: 1,
    WorkspaceRole.DEVELOPER: 2,
    WorkspaceRole.OWNER: 3,
}


async def authorize_workspace_access(
    db: AsyncSession,
    caller: Caller,
    workspace_id: uuid.UUID,
    min_role: WorkspaceRole = WorkspaceRole.VIEWER,
    not_found_detail: str = "Workspace not found",
) -> WorkspaceMembership:
    """Authorize caller access to a workspace based on workspace_memberships.

    Returns the WorkspaceMembership if caller is a member with at least min_role.
    If caller is NOT a member, raises 404 with not_found_detail (preventing cross-tenant leakage).
    If caller IS a member but has a lower role than min_role, raises 403 Forbidden.
    """
    stmt = select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_subject == caller.subject,
    )
    result = await db.execute(stmt)
    membership = result.scalar_one_or_none()

    if not membership:
        # Non-member receives 404 byte-identical to resource not found
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=not_found_detail,
        )

    if ROLE_RANK.get(membership.role, 0) < ROLE_RANK.get(min_role, 0):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient role for this operation",
        )

    return membership
