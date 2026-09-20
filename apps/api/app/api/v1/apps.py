import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, authorize_workspace_access, get_caller
from app.core.events import OutboxTopic
from app.core.idempotency import (
    check_idempotency,
    compute_payload_hash,
    create_idempotency_record,
    handle_idempotency_race,
)
from app.core.pagination import decode_cursor, encode_cursor
from app.db.session import get_db
from app.models.application import Application
from app.models.outbox import OutboxEvent, OutboxStatus
from app.models.release import Release, ReleaseStatus
from app.models.workspace import WorkspaceRole
from app.schemas.application import (
    ApplicationResponse,
    CreateApplicationRequest,
    DeployReleaseRequest,
    ReleaseListResponse,
    ReleaseResponse,
    RollbackRequest,
)
from app.schemas.common import AcceptedOperationResponse

router = APIRouter(tags=["Applications"])


@router.post(
    "/workspaces/{workspace_id}/apps",
    response_model=ApplicationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_application(
    workspace_id: uuid.UUID,
    payload: CreateApplicationRequest,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> ApplicationResponse:
    # Verify caller membership in workspace (requires DEVELOPER)
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.DEVELOPER, not_found_detail="Workspace not found"
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


@router.post(
    "/apps/{app_id}/deployments",
    response_model=AcceptedOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def deploy_release(
    app_id: uuid.UUID,
    payload: DeployReleaseRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=255),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> AcceptedOperationResponse:
    # Find application workspace_id without lock to verify existence and ownership
    # (scalar query avoids loading Application entity into ORM identity map before lock)
    ws_stmt = select(Application.workspace_id).where(Application.id == app_id)
    workspace_id = (await db.execute(ws_stmt)).scalar_one_or_none()
    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found",
        )

    # Tenant isolation: verify caller membership of application's workspace BEFORE taking row lock
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.DEVELOPER, not_found_detail="Application not found"
    )

    # Acquire exclusive row lock for safe generation increment (populate_existing=True ensures fresh entity)
    lock_stmt = (
        select(Application)
        .where(Application.id == app_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    app = (await db.execute(lock_stmt)).scalar_one()

    endpoint = f"/v1/apps/{app_id}/deployments"
    payload_hash = compute_payload_hash(payload)

    # 1. Idempotency Check
    cached_response = await check_idempotency(
        db, workspace_id, endpoint, idempotency_key, payload_hash
    )
    if cached_response:
        return cached_response

    # 2. Increment generation & determine release number (MAX(release_number) + 1 under row lock)
    rel_max_stmt = select(func.coalesce(func.max(Release.release_number), 0)).where(
        Release.application_id == app.id
    )
    release_max = (await db.execute(rel_max_stmt)).scalar() or 0
    next_release_number = release_max + 1

    app.desired_generation += 1

    release_id = uuid.uuid4()
    config_data = {
        "port": payload.port,
        "health_path": payload.health_path,
        "env_vars": payload.env_vars,
        "cpu_limit": payload.cpu_limit,
        "memory_limit": payload.memory_limit,
    }

    release = Release(
        id=release_id,
        application_id=app.id,
        workspace_id=workspace_id,
        release_number=next_release_number,
        image_digest=payload.image_digest,
        config_json=config_data,
        status=ReleaseStatus.IMAGE_READY,
    )
    db.add(release)

    # 3. Create Outbox Event
    event_id = uuid.uuid4()
    outbox_event = OutboxEvent(
        event_id=event_id,
        topic=OutboxTopic.APP_DEPLOYMENT_REQUESTED.value,
        payload_json={
            "event_id": str(event_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workspace_id": str(workspace_id),
            "application_id": str(app.id),
            "release_id": str(release_id),
            "generation": app.desired_generation,
            "image_digest": payload.image_digest,
            "port": payload.port,
            "health_path": payload.health_path,
        },
        headers_json={"idempotency_key": idempotency_key},
        status=OutboxStatus.PENDING,
    )
    db.add(outbox_event)

    # 4. Create Idempotency Record
    idemp_record = create_idempotency_record(
        workspace_id=workspace_id,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        response_code=202,
        operation_id=release_id,
    )
    db.add(idemp_record)

    try:
        await db.commit()
    except IntegrityError as exc:
        return await handle_idempotency_race(
            db, exc, workspace_id, endpoint, idempotency_key, payload_hash
        )

    return AcceptedOperationResponse(
        operation_id=release_id,
        status="ACCEPTED",
        status_url=f"/v1/operations/{release_id}",
    )


@router.post(
    "/apps/{app_id}/rollbacks",
    response_model=AcceptedOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rollback_release(
    app_id: uuid.UUID,
    payload: RollbackRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=255),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> AcceptedOperationResponse:
    # Find application workspace_id without lock to verify existence and ownership
    # (scalar query avoids loading Application entity into ORM identity map before lock)
    ws_stmt = select(Application.workspace_id).where(Application.id == app_id)
    workspace_id = (await db.execute(ws_stmt)).scalar_one_or_none()
    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found",
        )

    # Tenant isolation: verify caller membership of application's workspace BEFORE taking row lock
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.DEVELOPER, not_found_detail="Application not found"
    )

    # Acquire exclusive row lock for safe generation increment (populate_existing=True ensures fresh entity)
    lock_stmt = (
        select(Application)
        .where(Application.id == app_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    app = (await db.execute(lock_stmt)).scalar_one()

    target_rel_stmt = select(Release).where(
        Release.id == payload.target_release_id, Release.application_id == app.id
    )
    target_rel = (await db.execute(target_rel_stmt)).scalar_one_or_none()
    if not target_rel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target release not found for this application",
        )

    endpoint = f"/v1/apps/{app_id}/rollbacks"
    payload_hash = compute_payload_hash(payload)

    # 1. Idempotency Check
    cached_response = await check_idempotency(
        db, workspace_id, endpoint, idempotency_key, payload_hash
    )
    if cached_response:
        return cached_response

    # 2. Increment generation & determine release number
    app.desired_generation += 1

    rel_max_stmt = select(func.coalesce(func.max(Release.release_number), 0)).where(
        Release.application_id == app.id
    )
    release_max = (await db.execute(rel_max_stmt)).scalar() or 0
    next_release_number = release_max + 1

    new_release_id = uuid.uuid4()
    rollback_release_obj = Release(
        id=new_release_id,
        application_id=app.id,
        workspace_id=workspace_id,
        release_number=next_release_number,
        image_digest=target_rel.image_digest,
        config_json=target_rel.config_json,
        status=ReleaseStatus.IMAGE_READY,
    )
    db.add(rollback_release_obj)

    # Extract port and health_path from target release config
    target_config = target_rel.config_json or {}
    port = target_config.get("port", 8080)
    health_path = target_config.get("health_path", "/healthz")

    # 3. Insert outbox event
    event_id = uuid.uuid4()
    outbox_event = OutboxEvent(
        event_id=event_id,
        topic=OutboxTopic.APP_DEPLOYMENT_REQUESTED.value,
        payload_json={
            "event_id": str(event_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workspace_id": str(workspace_id),
            "application_id": str(app.id),
            "release_id": str(new_release_id),
            "generation": app.desired_generation,
            "image_digest": target_rel.image_digest,
            "port": port,
            "health_path": health_path,
            "is_rollback": True,
            "target_release_id": str(target_rel.id),
        },
        headers_json={"rollback_from": str(target_rel.id), "idempotency_key": idempotency_key},
        status=OutboxStatus.PENDING,
    )
    db.add(outbox_event)

    # 4. Create Idempotency Record
    idemp_record = create_idempotency_record(
        workspace_id=workspace_id,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        response_code=202,
        operation_id=new_release_id,
    )
    db.add(idemp_record)

    try:
        await db.commit()
    except IntegrityError as exc:
        return await handle_idempotency_race(
            db, exc, workspace_id, endpoint, idempotency_key, payload_hash
        )

    return AcceptedOperationResponse(
        operation_id=new_release_id,
        status="ACCEPTED",
        status_url=f"/v1/operations/{new_release_id}",
    )


@router.get(
    "/apps/{app_id}/releases",
    response_model=ReleaseListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_releases(
    app_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> ReleaseListResponse:
    # 1. Look up application
    app_stmt = select(Application).where(Application.id == app_id)
    app = (await db.execute(app_stmt)).scalar_one_or_none()
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found",
        )

    # 2. Authorize workspace access (VIEWER or higher)
    await authorize_workspace_access(
        db, caller, app.workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Application not found"
    )

    # 3. Query releases with cursor pagination
    stmt = select(Release).where(Release.application_id == app_id)
    if cursor:
        cursor_ts, cursor_id = decode_cursor(cursor)
        stmt = stmt.where(
            (Release.created_at < cursor_ts)
            | ((Release.created_at == cursor_ts) & (Release.id < cursor_id))
        )

    stmt = stmt.order_by(Release.created_at.desc(), Release.id.desc()).limit(limit + 1)
    releases = (await db.execute(stmt)).scalars().all()

    next_cursor = None
    if len(releases) > limit:
        next_item = releases[limit - 1]
        next_cursor = encode_cursor(next_item.created_at, next_item.id)
        releases = releases[:limit]

    items = [
        ReleaseResponse(
            id=r.id,
            application_id=r.application_id,
            workspace_id=r.workspace_id,
            release_number=r.release_number,
            image_digest=r.image_digest,
            config_json=r.config_json or {},
            status=r.status,
            created_at=r.created_at,
        )
        for r in releases
    ]
    return ReleaseListResponse(items=items, next_cursor=next_cursor)
