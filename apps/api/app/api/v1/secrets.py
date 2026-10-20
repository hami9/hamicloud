import uuid
from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_audit_event
from app.core.auth import Caller, authorize_workspace_access, get_caller
from app.core.crypto import encrypt_secret
from app.db.session import get_db
from app.models.secret import SecretReference
from app.models.workspace import WorkspaceRole
from app.schemas.secret import (
    SecretListResponse,
    SecretMetadataResponse,
    UpsertSecretRequest,
)

router = APIRouter(tags=["Secrets"])


@router.post(
    "/workspaces/{workspace_id}/secrets",
    response_model=SecretMetadataResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_workspace_secret(
    workspace_id: uuid.UUID,
    payload: UpsertSecretRequest,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> SecretMetadataResponse:
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.DEVELOPER, not_found_detail="Workspace not found"
    )

    encrypted = encrypt_secret(payload.value, workspace_id=workspace_id, secret_name=payload.name)

    stmt = select(SecretReference).where(
        SecretReference.workspace_id == workspace_id,
        SecretReference.name == payload.name,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        existing.encrypted_value = encrypted
        existing.key_version += 1
        secret_ref = existing
        audit_action = "secret.rotated"
    else:
        secret_ref = SecretReference(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            name=payload.name,
            encrypted_value=encrypted,
            key_version=1,
        )
        db.add(secret_ref)
        audit_action = "secret.created"

    record_audit_event(
        db=db,
        workspace_id=workspace_id,
        actor_subject=caller.subject,
        action=audit_action,
        target_type="SECRET",
        target_id=payload.name,
        details={"name": payload.name, "key_version": secret_ref.key_version},
    )

    await db.commit()
    await db.refresh(secret_ref)

    return SecretMetadataResponse(
        id=secret_ref.id,
        workspace_id=secret_ref.workspace_id,
        name=secret_ref.name,
        key_version=secret_ref.key_version,
        created_at=secret_ref.created_at,
        updated_at=secret_ref.updated_at,
    )


@router.get(
    "/workspaces/{workspace_id}/secrets",
    response_model=SecretListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_workspace_secrets(
    workspace_id: uuid.UUID,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> SecretListResponse:
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Workspace not found"
    )

    stmt = (
        select(SecretReference)
        .where(SecretReference.workspace_id == workspace_id)
        .order_by(SecretReference.name.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()

    return SecretListResponse(
        items=[
            SecretMetadataResponse(
                id=s.id,
                workspace_id=s.workspace_id,
                name=s.name,
                key_version=s.key_version,
                created_at=s.created_at,
                updated_at=s.updated_at,
            )
            for s in rows
        ]
    )


@router.delete(
    "/workspaces/{workspace_id}/secrets/{secret_name}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_workspace_secret(
    workspace_id: uuid.UUID,
    secret_name: str = Path(..., min_length=1, max_length=100),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> None:
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.ADMIN, not_found_detail="Workspace not found"
    )

    stmt = select(SecretReference).where(
        SecretReference.workspace_id == workspace_id,
        SecretReference.name == secret_name,
    )
    secret_ref = (await db.execute(stmt)).scalar_one_or_none()
    if not secret_ref:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Secret not found",
        )

    await db.delete(secret_ref)
    record_audit_event(
        db=db,
        workspace_id=workspace_id,
        actor_subject=caller.subject,
        action="secret.deleted",
        target_type="SECRET",
        target_id=secret_name,
        details={"name": secret_name},
    )
    await db.commit()
