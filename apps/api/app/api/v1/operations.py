import uuid
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, authorize_workspace_access, get_caller
from app.db.session import get_db
from app.models.job import Job
from app.models.release import Release
from app.models.workspace import WorkspaceRole
from app.schemas.common import OperationKind, OperationStatus, OperationStatusResponse

router = APIRouter(tags=["Operations"])


@router.get(
    "/operations/{operation_id}",
    response_model=OperationStatusResponse,
    status_code=status.HTTP_200_OK,
)
async def get_operation_status(
    operation_id: uuid.UUID,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> OperationStatusResponse:
    """Inspect current status of a background operation (Job or Release)."""
    # 1. Check if operation is a Release
    rel_stmt = select(Release).where(Release.id == operation_id)
    rel = (await db.execute(rel_stmt)).scalar_one_or_none()
    if rel:
        await authorize_workspace_access(
            db, caller, rel.workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Operation not found"
        )
        return OperationStatusResponse(
            operation_id=rel.id,
            operation_kind=OperationKind.RELEASE,
            status=OperationStatus(rel.status.value),
            status_url=f"/v1/operations/{rel.id}",
            created_at=rel.created_at,
        )

    # 2. Check if operation is a Job
    job_stmt = select(Job).where(Job.id == operation_id)
    job = (await db.execute(job_stmt)).scalar_one_or_none()
    if job:
        await authorize_workspace_access(
            db, caller, job.workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Operation not found"
        )
        return OperationStatusResponse(
            operation_id=job.id,
            operation_kind=OperationKind.JOB,
            status=OperationStatus(job.state.value),
            status_url=f"/v1/operations/{job.id}",
            created_at=job.created_at,
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Operation not found",
    )


@router.get(
    "/operations/{operation_id}/events",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
async def stream_operation_events(
    operation_id: uuid.UUID,
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Server-Sent Events (SSE) stream for operation logs and events.

    Authorized SSE stream with reconnect cursor (Last-Event-ID) and bounded retention.
    In M0 design baseline, returns 501 Not Implemented until live event streaming lands in M1.
    """
    # Verify operation exists and caller is authorized member of its workspace
    rel_stmt = select(Release).where(Release.id == operation_id)
    rel = (await db.execute(rel_stmt)).scalar_one_or_none()
    if rel:
        await authorize_workspace_access(
            db, caller, rel.workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Operation not found"
        )
    else:
        job_stmt = select(Job).where(Job.id == operation_id)
        job = (await db.execute(job_stmt)).scalar_one_or_none()
        if job:
            await authorize_workspace_access(
                db, caller, job.workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Operation not found"
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Operation not found",
            )

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Server-Sent Events streaming is not yet implemented in M0 baseline",
    )
