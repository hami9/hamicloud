import uuid
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, authorize_workspace_access, get_caller
from app.core.telemetry import telemetry_collector
from app.db.session import get_db
from app.models.workspace import WorkspaceRole
from app.schemas.telemetry import WorkspaceTelemetryResponse

router = APIRouter(tags=["Observability"])


@router.get(
    "/workspaces/{workspace_id}/telemetry",
    response_model=WorkspaceTelemetryResponse,
    status_code=status.HTTP_200_OK,
)
async def get_workspace_telemetry(
    workspace_id: uuid.UUID,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceTelemetryResponse:
    """Inspect workspace-scoped operational telemetry, queue depths, active leases, and API latency."""
    await authorize_workspace_access(
        db,
        caller,
        workspace_id,
        min_role=WorkspaceRole.VIEWER,
        not_found_detail="Workspace not found",
    )
    metrics_data = await telemetry_collector.collect_metrics(db=db, workspace_id=workspace_id)
    return WorkspaceTelemetryResponse(
        workspace_id=workspace_id,
        **metrics_data,
    )
