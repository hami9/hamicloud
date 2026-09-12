import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.idempotency import IdempotencyRecord
from app.models.job import Job, JobAttempt, JobState
from app.models.outbox import OutboxEvent, OutboxStatus
from app.models.workspace import Workspace
from app.schemas.common import AcceptedOperationResponse
from app.schemas.job import JobAttemptItem, JobDetailsResponse, SubmitJobRequest

router = APIRouter(tags=["Jobs"])


@router.post(
    "/workspaces/{workspace_id}/jobs",
    response_model=AcceptedOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_job(
    workspace_id: uuid.UUID,
    payload: SubmitJobRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
) -> AcceptedOperationResponse:
    endpoint = f"/v1/workspaces/{workspace_id}/jobs"
    payload_dict = payload.model_dump(mode="json")
    payload_hash = hashlib.sha256(
        json.dumps(payload_dict, sort_keys=True).encode("utf-8")
    ).hexdigest()

    # 1. Check Idempotency Record
    idemp_stmt = select(IdempotencyRecord).where(
        IdempotencyRecord.workspace_id == workspace_id,
        IdempotencyRecord.endpoint == endpoint,
        IdempotencyRecord.idempotency_key == idempotency_key,
    )
    existing_idemp = (await db.execute(idemp_stmt)).scalar_one_or_none()
    if existing_idemp:
        if existing_idemp.request_hash == payload_hash:
            # Same request: return stored idempotent response
            return AcceptedOperationResponse(
                operation_id=uuid.UUID(existing_idemp.response_body["operation_id"]),
                status=existing_idemp.response_body["status"],
                status_url=existing_idemp.response_body["status_url"],
            )
        else:
            # Reused key with different payload: 409 Conflict
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key reused with different request payload",
            )

    # 2. Check workspace existence
    ws_stmt = select(Workspace).where(Workspace.id == workspace_id)
    ws = (await db.execute(ws_stmt)).scalar_one_or_none()
    if not ws:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        )

    # 3. Create Job, Outbox Event, and Idempotency Record in ONE atomic transaction
    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        workspace_id=workspace_id,
        name=payload.name,
        image_digest=payload.image_digest,
        command_args=payload.command_args,
        env_vars=payload.env_vars,
        timeout_seconds=payload.timeout_seconds,
        max_retries=payload.max_retries,
        state=JobState.QUEUED,
    )
    db.add(job)

    # Prepare outbox event
    event_id = uuid.uuid4()
    outbox_event = OutboxEvent(
        event_id=event_id,
        topic="job.submitted.v1",
        payload_json={
            "event_id": str(event_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workspace_id": str(workspace_id),
            "job_id": str(job_id),
            "name": payload.name,
            "image_digest": payload.image_digest,
            "command_args": payload.command_args,
            "timeout_seconds": payload.timeout_seconds,
            "max_retries": payload.max_retries,
        },
        headers_json={"idempotency_key": idempotency_key},
        status=OutboxStatus.PENDING,
    )
    db.add(outbox_event)

    # Prepare response data
    response_data = {
        "operation_id": str(job_id),
        "status": "ACCEPTED",
        "status_url": f"/v1/jobs/{job_id}",
    }

    # Prepare idempotency record
    idemp_record = IdempotencyRecord(
        workspace_id=workspace_id,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=payload_hash,
        response_code=202,
        response_body=response_data,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db.add(idemp_record)

    # Commit single atomic transaction
    await db.commit()

    return AcceptedOperationResponse(
        operation_id=job_id,
        status="ACCEPTED",
        status_url=f"/v1/jobs/{job_id}",
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobDetailsResponse,
    status_code=status.HTTP_200_OK,
)
async def get_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> JobDetailsResponse:
    stmt = select(Job).where(Job.id == job_id)
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    # Load attempts
    attempts_stmt = (
        select(JobAttempt)
        .where(JobAttempt.job_id == job_id)
        .order_by(JobAttempt.attempt_number)
    )
    attempts_result = await db.execute(attempts_stmt)
    attempts = attempts_result.scalars().all()

    return JobDetailsResponse(
        id=job.id,
        workspace_id=job.workspace_id,
        name=job.name,
        state=job.state,
        current_attempt_number=job.current_attempt_number,
        attempts=[
            JobAttemptItem(
                attempt_number=att.attempt_number,
                state=att.state,
                resource_uid=att.resource_uid,
                lease_epoch=att.lease_epoch,
                exit_code=att.exit_code,
                failure_reason=att.failure_reason,
                started_at=att.started_at,
                finished_at=att.finished_at,
            )
            for att in attempts
        ],
        created_at=job.created_at,
    )
