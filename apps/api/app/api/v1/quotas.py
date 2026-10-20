import uuid
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, authorize_workspace_access, get_caller
from app.core.config import settings
from app.core.quotas import get_workspace_quota_summary
from app.db.session import get_db
from app.models.workspace import WorkspaceRole
from app.schemas.quota import QuotaReservationItem, WorkspaceQuotaResponse

router = APIRouter(tags=["Quotas"])


@router.get(
    "/workspaces/{workspace_id}/quotas",
    response_model=WorkspaceQuotaResponse,
    status_code=status.HTTP_200_OK,
)
async def get_workspace_quotas(
    workspace_id: uuid.UUID,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceQuotaResponse:
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Workspace not found"
    )

    (
        jobs_used,
        builds_used,
        services_used,
        pending_used,
        reservations,
    ) = await get_workspace_quota_summary(db, workspace_id)
    await db.commit()

    return WorkspaceQuotaResponse(
        workspace_id=workspace_id,
        concurrent_jobs_limit=settings.DEFAULT_WORKSPACE_JOB_CONCURRENCY,
        concurrent_jobs_used=jobs_used,
        concurrent_builds_limit=settings.DEFAULT_WORKSPACE_BUILD_CONCURRENCY,
        concurrent_builds_used=builds_used,
        deployed_services_limit=settings.DEFAULT_WORKSPACE_SERVICE_CAP,
        deployed_services_used=services_used,
        pending_operations_limit=settings.DEFAULT_WORKSPACE_PENDING_OPERATIONS,
        pending_operations_used=pending_used,
        reservations=[
            QuotaReservationItem(
                id=r.id,
                workspace_id=r.workspace_id,
                resource_class=r.resource_class,
                units=r.units,
                status=r.status,
                expires_at=r.expires_at,
                created_at=r.created_at,
            )
            for r in reservations
        ],
    )
