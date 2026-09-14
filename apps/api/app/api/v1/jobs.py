import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, authorize_workspace_access, get_caller
from app.core.events import OutboxTopic
from app.db.session import get_db
from app.models.idempotency import IdempotencyRecord
from app.models.job import Job, JobAttempt, JobState
from app.models.outbox import OutboxEvent, OutboxStatus
from app.models.workspace import WorkspaceRole
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
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> AcceptedOperationResponse:
    # 1. Authorize workspace membership (DEVELOPER role required)
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.DEVELOPER, not_found_detail="Workspace not found"
    )

    endpoint = f"/v1/workspaces/{workspace_id}/jobs"
    payload_dict = payload.model_dump(mode="json")
    payload_hash = hashlib.sha256(
        json.dumps(payload_dict, sort_keys=True).encode("utf-8")
    ).hexdigest()

    # 2. Check Idempotency Record
    idemp_stmt = select(IdempotencyRecord).where(
        IdempotencyRecord.workspace_id == workspace_id,
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
                detail="Idempotency key reused with different request payload",
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
        topic=OutboxTopic.JOB_SUBMITTED.value,
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
        "status_url": f"/v1/operations/{job_id}",
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

    # Commit single atomic transaction with race condition handling
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
            detail="Concurrent idempotency conflict detected",
        )

    return AcceptedOperationResponse(
        operation_id=job_id,
        status="ACCEPTED",
        status_url=f"/v1/operations/{job_id}",
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobDetailsResponse,
    status_code=status.HTTP_200_OK,
)
async def get_job(
    job_id: uuid.UUID,
    caller: Caller = Depends(get_caller),
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

    # Tenant isolation: verify caller membership of job's workspace (VIEWER or higher)
    # Non-member receives 404 byte-identical to missing job ID
    await authorize_workspace_access(
        db, caller, job.workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Job not found"
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


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=AcceptedOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_job(
    job_id: uuid.UUID,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> AcceptedOperationResponse:
    stmt = select(Job).where(Job.id == job_id)
    job = (await db.execute(stmt)).scalar_one_or_none()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    # Tenant isolation: verify caller membership of job's workspace (DEVELOPER or higher)
    await authorize_workspace_access(
        db, caller, job.workspace_id, min_role=WorkspaceRole.DEVELOPER, not_found_detail="Job not found"
    )

    # Terminal jobs cannot be cancelled
    if job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot cancel job in terminal state '{job.state.value}'",
        )

    job.state = JobState.CANCEL_REQUESTED

    # Insert outbox event for cancellation
    event_id = uuid.uuid4()
    outbox_event = OutboxEvent(
        event_id=event_id,
        topic=OutboxTopic.JOB_CANCELLATION_REQUESTED.value,
        payload_json={
            "event_id": str(event_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workspace_id": str(job.workspace_id),
            "job_id": str(job.id),
        },
        headers_json={},
        status=OutboxStatus.PENDING,
    )
    db.add(outbox_event)
    await db.commit()

    return AcceptedOperationResponse(
        operation_id=job.id,
        status="ACCEPTED",
        status_url=f"/v1/operations/{job.id}",
    )


@router.post(
    "/jobs/{job_id}/reruns",
    response_model=AcceptedOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rerun_job(
    job_id: uuid.UUID,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> AcceptedOperationResponse:
    stmt = select(Job).where(Job.id == job_id)
    original_job = (await db.execute(stmt)).scalar_one_or_none()
    if not original_job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    # Tenant isolation: verify caller membership of job's workspace (DEVELOPER or higher)
    await authorize_workspace_access(
        db, caller, original_job.workspace_id, min_role=WorkspaceRole.DEVELOPER, not_found_detail="Job not found"
    )

    if original_job.state not in (JobState.FAILED, JobState.CANCELLED, JobState.SUCCEEDED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot rerun job while active in state '{original_job.state.value}'",
        )

    # Create new logical job linked to predecessor
    new_job_id = uuid.uuid4()
    new_job = Job(
        id=new_job_id,
        workspace_id=original_job.workspace_id,
        name=f"{original_job.name}-rerun",
        image_digest=original_job.image_digest,
        command_args=original_job.command_args,
        env_vars=original_job.env_vars,
        timeout_seconds=original_job.timeout_seconds,
        max_retries=original_job.max_retries,
        state=JobState.QUEUED,
    )
    db.add(new_job)

    # Insert outbox event
    event_id = uuid.uuid4()
    outbox_event = OutboxEvent(
        event_id=event_id,
        topic=OutboxTopic.JOB_SUBMITTED.value,
        payload_json={
            "event_id": str(event_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workspace_id": str(original_job.workspace_id),
            "job_id": str(new_job_id),
            "name": new_job.name,
            "image_digest": new_job.image_digest,
            "command_args": new_job.command_args,
            "timeout_seconds": new_job.timeout_seconds,
            "max_retries": new_job.max_retries,
            "parent_job_id": str(original_job.id),
        },
        headers_json={"rerun_from": str(original_job.id)},
        status=OutboxStatus.PENDING,
    )
    db.add(outbox_event)
    await db.commit()

    return AcceptedOperationResponse(
        operation_id=new_job_id,
        status="ACCEPTED",
        status_url=f"/v1/operations/{new_job_id}",
    )
