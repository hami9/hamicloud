import uuid
from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit_event
from app.core.auth import Caller, authorize_workspace_access, get_caller
from app.core.db_errors import unexpected_integrity_error, violated_constraint
from app.db.session import get_db
from app.models.workspace import Workspace, WorkspaceMembership, WorkspaceRole
from app.schemas.workspace import (
    CreateWorkspaceRequest,
    RemoveWorkspaceMemberResponse,
    UpsertWorkspaceMemberRequest,
    WorkspaceMemberListResponse,
    WorkspaceMemberResponse,
    WorkspaceResponse,
)

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
        await db.flush()
        record_audit_event(
            db=db,
            workspace_id=workspace.id,
            actor_subject=caller.subject,
            action="workspace.created",
            target_type="WORKSPACE",
            target_id=str(workspace.id),
            details={"slug": workspace.slug, "name": workspace.name},
        )
        await db.commit()
    except IntegrityError as exc:
        # The slug pre-check above is not atomic: a concurrent request may have
        # inserted the same slug in between. Report the documented 409 rather
        # than letting the unique violation surface as a 500.
        await db.rollback()
        if violated_constraint(exc) != "uq_workspace_slug":
            raise unexpected_integrity_error(exc, "createWorkspace") from exc
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


@router.get(
    "/{workspace_id}",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_200_OK,
)
async def get_workspace(
    workspace_id: uuid.UUID,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceResponse:
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Workspace not found"
    )
    ws = (
        await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    if not ws:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )
    return WorkspaceResponse(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        created_at=ws.created_at,
    )


@router.get(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_workspace_members(
    workspace_id: uuid.UUID,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceMemberListResponse:
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Workspace not found"
    )
    stmt = (
        select(WorkspaceMembership)
        .where(WorkspaceMembership.workspace_id == workspace_id)
        .order_by(WorkspaceMembership.created_at.asc())
    )
    members = (await db.execute(stmt)).scalars().all()
    return WorkspaceMemberListResponse(
        items=[
            WorkspaceMemberResponse(
                id=m.id,
                workspace_id=m.workspace_id,
                user_subject=m.user_subject,
                role=m.role,
                created_at=m.created_at,
            )
            for m in members
        ]
    )


@router.post(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_workspace_member(
    workspace_id: uuid.UUID,
    payload: UpsertWorkspaceMemberRequest,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceMemberResponse:
    caller_membership = await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.ADMIN, not_found_detail="Workspace not found"
    )

    if payload.role == WorkspaceRole.OWNER and caller_membership.role != WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only workspace OWNER can grant OWNER role",
        )

    stmt = select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_subject == payload.user_subject.strip(),
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        if existing.role == WorkspaceRole.OWNER and caller_membership.role != WorkspaceRole.OWNER:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace OWNER can modify an existing OWNER membership",
            )
        if existing.role == WorkspaceRole.OWNER and payload.role != WorkspaceRole.OWNER:
            owner_count = (
                await db.execute(
                    select(func.count())
                    .select_from(WorkspaceMembership)
                    .where(
                        WorkspaceMembership.workspace_id == workspace_id,
                        WorkspaceMembership.role == WorkspaceRole.OWNER,
                    )
                )
            ).scalar() or 0
            if owner_count <= 1:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Cannot demote the last OWNER of a workspace",
                )
        existing.role = payload.role
        member = existing
    else:
        member = WorkspaceMembership(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            user_subject=payload.user_subject.strip(),
            role=payload.role,
        )
        db.add(member)

    record_audit_event(
        db=db,
        workspace_id=workspace_id,
        actor_subject=caller.subject,
        action="workspace.member.upserted",
        target_type="WORKSPACE_MEMBER",
        target_id=payload.user_subject.strip(),
        details={"user_subject": payload.user_subject.strip(), "role": payload.role.value},
    )

    await db.commit()
    await db.refresh(member)

    return WorkspaceMemberResponse(
        id=member.id,
        workspace_id=member.workspace_id,
        user_subject=member.user_subject,
        role=member.role,
        created_at=member.created_at,
    )


@router.delete(
    "/{workspace_id}/members/{user_subject}",
    response_model=RemoveWorkspaceMemberResponse,
    status_code=status.HTTP_200_OK,
)
async def remove_workspace_member(
    workspace_id: uuid.UUID,
    user_subject: str = Path(..., min_length=1, max_length=255),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> RemoveWorkspaceMemberResponse:
    caller_membership = await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.ADMIN, not_found_detail="Workspace not found"
    )

    stmt = select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_subject == user_subject,
    )
    target = (await db.execute(stmt)).scalar_one_or_none()
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace member not found",
        )

    if target.role == WorkspaceRole.OWNER:
        if caller_membership.role != WorkspaceRole.OWNER:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace OWNER can remove an OWNER member",
            )
        owner_count = (
            await db.execute(
                select(func.count())
                .select_from(WorkspaceMembership)
                .where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.role == WorkspaceRole.OWNER,
                )
            )
        ).scalar() or 0
        if owner_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Cannot remove the last OWNER of a workspace",
            )

    await db.delete(target)
    record_audit_event(
        db=db,
        workspace_id=workspace_id,
        actor_subject=caller.subject,
        action="workspace.member.removed",
        target_type="WORKSPACE_MEMBER",
        target_id=user_subject,
        details={"user_subject": user_subject, "previous_role": target.role.value},
    )
    await db.commit()

    return RemoveWorkspaceMemberResponse(
        status="REMOVED",
        user_subject=user_subject,
    )
