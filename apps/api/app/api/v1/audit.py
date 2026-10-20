import uuid
from typing import Optional
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, authorize_workspace_access, get_caller
from app.core.pagination import decode_cursor, encode_cursor
from app.db.session import get_db
from app.models.audit import AuditEvent
from app.models.workspace import WorkspaceRole
from app.schemas.audit import AuditEventItem, AuditEventListResponse

router = APIRouter(tags=["Audit"])


@router.get(
    "/workspaces/{workspace_id}/audit",
    response_model=AuditEventListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_workspace_audit_events(
    workspace_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None, max_length=100),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> AuditEventListResponse:
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.ADMIN, not_found_detail="Workspace not found"
    )

    stmt = select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)

    if cursor is not None:
        cursor_dt, cursor_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                AuditEvent.created_at < cursor_dt,
                and_(
                    AuditEvent.created_at == cursor_dt,
                    AuditEvent.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())

    next_cursor: Optional[str] = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return AuditEventListResponse(
        items=[
            AuditEventItem(
                id=ev.id,
                workspace_id=ev.workspace_id,
                actor_subject=ev.actor_subject,
                action=ev.action,
                target_type=ev.target_type,
                target_id=ev.target_id,
                result=ev.result,
                details_json=ev.details_json or {},
                created_at=ev.created_at,
            )
            for ev in rows
        ],
        next_cursor=next_cursor,
    )
