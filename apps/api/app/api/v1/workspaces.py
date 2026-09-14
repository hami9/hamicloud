import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, get_caller
from app.db.session import get_db
from app.models.workspace import Workspace, WorkspaceMembership, WorkspaceRole
from app.schemas.workspace import CreateWorkspaceRequest, WorkspaceResponse

router = APIRouter(prefix="/workspaces", tags=["Workspaces"])


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: CreateWorkspaceRequest,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceResponse:
    # Check if slug exists
    stmt = select(Workspace).where(Workspace.slug == payload.slug)
    result = await db.execute(stmt)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Workspace with slug '{payload.slug}' already exists",
        )

    workspace = Workspace(name=payload.name, slug=payload.slug)
    db.add(workspace)
    await db.flush()

    # Record caller as OWNER
    membership = WorkspaceMembership(
        workspace_id=workspace.id,
        user_subject=caller.subject,
        role=WorkspaceRole.OWNER,
    )
    db.add(membership)
    await db.commit()
    await db.refresh(workspace)

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        created_at=workspace.created_at,
    )
