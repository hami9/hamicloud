import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.application import Application
from app.models.idempotency import IdempotencyRecord
from app.models.outbox import OutboxEvent, OutboxStatus
from app.models.release import Release, ReleaseStatus
from app.models.workspace import Workspace
from app.schemas.application import (
    ApplicationResponse,
    CreateApplicationRequest,
    DeployReleaseRequest,
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


@router.post(
    "/apps/{app_id}/deployments",
    response_model=AcceptedOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def deploy_release(
    app_id: uuid.UUID,
    payload: DeployReleaseRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
) -> AcceptedOperationResponse:
    # Find application
    app_stmt = select(Application).where(Application.id == app_id)
    app = (await db.execute(app_stmt)).scalar_one_or_none()
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found",
        )

    endpoint = f"/v1/apps/{app_id}/deployments"
    payload_dict = payload.model_dump(mode="json")
    payload_hash = hashlib.sha256(
        json.dumps(payload_dict, sort_keys=True).encode("utf-8")
    ).hexdigest()

    # 1. Idempotency Check
    idemp_stmt = select(IdempotencyRecord).where(
        IdempotencyRecord.workspace_id == app.workspace_id,
        IdempotencyRecord.endpoint == endpoint,
        IdempotencyRecord.idempotency_key == idempotency_key,
    )
    existing_idemp = (await db.execute(idemp_stmt)).scalar_one_or_none()
    if existing_idemp:
        if existing_idemp.request_hash == payload_hash:
            return AcceptedOperationResponse(
                operation_id=uuid.UUID(existing_idemp.response_body["operation_id"]),
                status=existing_idemp.response_body["status"],
                status_url=existing_idemp.response_body["status_url"],
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key reused with different deployment payload",
            )

    # 2. Increment generation & determine release number
    rel_count_stmt = select(func.count(Release.id)).where(Release.application_id == app.id)
    release_count = (await db.execute(rel_count_stmt)).scalar() or 0
    next_release_number = release_count + 1

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
        workspace_id=app.workspace_id,
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
        topic="app.deployment.requested.v1",
        payload_json={
            "event_id": str(event_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workspace_id": str(app.workspace_id),
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
    response_data = {
        "operation_id": str(release_id),
        "status": "ACCEPTED",
        "status_url": f"/v1/operations/{release_id}",
    }
    idemp_record = IdempotencyRecord(
        workspace_id=app.workspace_id,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=payload_hash,
        response_code=202,
        response_body=response_data,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db.add(idemp_record)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        concurrent_idemp = (await db.execute(idemp_stmt)).scalar_one_or_none()
        if concurrent_idemp and concurrent_idemp.request_hash == payload_hash:
            return AcceptedOperationResponse(
                operation_id=uuid.UUID(concurrent_idemp.response_body["operation_id"]),
                status=concurrent_idemp.response_body["status"],
                status_url=concurrent_idemp.response_body["status_url"],
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent deployment conflict detected",
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
    db: AsyncSession = Depends(get_db),
) -> AcceptedOperationResponse:
    app_stmt = select(Application).where(Application.id == app_id)
    app = (await db.execute(app_stmt)).scalar_one_or_none()
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found",
        )

    target_rel_stmt = select(Release).where(
        Release.id == payload.target_release_id, Release.application_id == app.id
    )
    target_rel = (await db.execute(target_rel_stmt)).scalar_one_or_none()
    if not target_rel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target release not found for this application",
        )

    # Increment generation
    app.desired_generation += 1

    rel_count_stmt = select(func.count(Release.id)).where(Release.application_id == app.id)
    release_count = (await db.execute(rel_count_stmt)).scalar() or 0
    next_release_number = release_count + 1

    new_release_id = uuid.uuid4()
    rollback_release = Release(
        id=new_release_id,
        application_id=app.id,
        workspace_id=app.workspace_id,
        release_number=next_release_number,
        image_digest=target_rel.image_digest,
        config_json=target_rel.config_json,
        status=ReleaseStatus.IMAGE_READY,
    )
    db.add(rollback_release)

    # Insert outbox event
    event_id = uuid.uuid4()
    outbox_event = OutboxEvent(
        event_id=event_id,
        topic="app.deployment.requested.v1",
        payload_json={
            "event_id": str(event_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workspace_id": str(app.workspace_id),
            "application_id": str(app.id),
            "release_id": str(new_release_id),
            "generation": app.desired_generation,
            "image_digest": target_rel.image_digest,
            "is_rollback": True,
            "target_release_id": str(target_rel.id),
        },
        headers_json={"rollback_from": str(target_rel.id)},
        status=OutboxStatus.PENDING,
    )
    db.add(outbox_event)
    await db.commit()

    return AcceptedOperationResponse(
        operation_id=new_release_id,
        status="ACCEPTED",
        status_url=f"/v1/operations/{new_release_id}",
    )
