import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.application import Application
from app.models.workspace import Workspace
from app.schemas.application import ApplicationResponse, CreateApplicationRequest

router = APIRouter(tags=["Applications"])


@router.post(
    "/workspaces/{workspace_id}/apps",
    response_model=ApplicationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_application(
    workspace_id: uuid.UUID,
    payload: CreateApplicationRequest,
    db: AsyncSession = Depends(get_db),
) -> ApplicationResponse:
    # Verify workspace exists
    ws_stmt = select(Workspace).where(Workspace.id == workspace_id)
    ws = (await db.execute(ws_stmt)).scalar_one_or_none()
    if not ws:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )

    # Check slug uniqueness within workspace
    app_stmt = select(Application).where(
        Application.workspace_id == workspace_id, Application.slug == payload.slug
    )
    existing_app = (await db.execute(app_stmt)).scalar_one_or_none()
    if existing_app:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Application with slug '{payload.slug}' already exists in this workspace",
        )

    app = Application(
        workspace_id=workspace_id,
        name=payload.name,
        slug=payload.slug,
        workload_type=payload.workload_type,
        desired_generation=1,
    )
    db.add(app)
    await db.commit()
    await db.refresh(app)

    return ApplicationResponse(
        id=app.id,
        workspace_id=app.workspace_id,
        name=app.name,
        slug=app.slug,
        workload_type=app.workload_type.value,
        desired_generation=app.desired_generation,
        current_release_id=app.current_release_id,
        created_at=app.created_at,
    )
