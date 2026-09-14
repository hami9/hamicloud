import uuid
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.job import Job
from app.models.release import Release
from app.schemas.common import OperationKind, OperationStatus, OperationStatusResponse

router = APIRouter(tags=["Operations"])


@router.get(
    "/operations/{operation_id}",
    response_model=OperationStatusResponse,
    status_code=status.HTTP_200_OK,
)
async def get_operation_status(
    operation_id: uuid.UUID,
    x_workspace_id: Optional[str] = Header(None, alias="X-Workspace-ID"),
    db: AsyncSession = Depends(get_db),
) -> OperationStatusResponse:
    """Inspect current status of a background operation (Job or Release)."""
    expected_ws_id = None
    if x_workspace_id:
        try:
            expected_ws_id = uuid.UUID(x_workspace_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid X-Workspace-ID header format",
            )

    # 1. Check if operation is a Release
    rel_stmt = select(Release).where(Release.id == operation_id)
    rel = (await db.execute(rel_stmt)).scalar_one_or_none()
    if rel:
        if expected_ws_id and rel.workspace_id != expected_ws_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Operation not found in specified workspace",
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
        if expected_ws_id and job.workspace_id != expected_ws_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Operation not found in specified workspace",
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
    x_workspace_id: Optional[str] = Header(None, alias="X-Workspace-ID"),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Server-Sent Events (SSE) stream for operation logs and events.

    Authorized SSE stream with reconnect cursor (Last-Event-ID) and bounded retention.
    In M0 design baseline, returns 501 Not Implemented until live event streaming lands in M1.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Server-Sent Events streaming is not yet implemented in M0 baseline",
    )
