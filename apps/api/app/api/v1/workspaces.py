import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, get_caller
from app.core.db_errors import violated_constraint
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

    workspace = Workspace(id=uuid.uuid4(), name=payload.name, slug=payload.slug)
    db.add(workspace)

    # Record caller as OWNER
    membership = WorkspaceMembership(
        workspace_id=workspace.id,
        user_subject=caller.subject,
        role=WorkspaceRole.OWNER,
    )
    db.add(membership)
    try:
        await db.commit()
    except IntegrityError as exc:
        # The slug pre-check above is not atomic: a concurrent request may have
        # inserted the same slug in between. Report the documented 409 rather
        # than letting the unique violation surface as a 500.
        await db.rollback()
        if violated_constraint(exc) != "uq_workspace_slug":
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Workspace with slug '{payload.slug}' already exists",
        ) from exc
    await db.refresh(workspace)

    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        created_at=workspace.created_at,
    )
