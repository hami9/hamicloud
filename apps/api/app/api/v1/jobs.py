import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import Caller, authorize_workspace_access, get_caller
from app.core.events import OutboxTopic, create_outbox_event
from app.core.idempotency import (
    check_idempotency,
    compute_payload_hash,
    create_idempotency_record,
    get_idempotency_key,
    handle_idempotency_race,
)
from app.core.pagination import decode_cursor, encode_cursor
from app.db.session import get_db
from app.models.job import Job, JobAttempt, JobState
from app.models.workspace import WorkspaceRole
from app.schemas.common import AcceptedOperationResponse
from app.schemas.job import JobAttemptItem, JobDetailsResponse, JobListResponse, SubmitJobRequest

router = APIRouter(tags=["Jobs"])


@router.post(
    "/workspaces/{workspace_id}/jobs",
    response_model=AcceptedOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_job(
    workspace_id: uuid.UUID,
    payload: SubmitJobRequest,
    idempotency_key: str = Depends(get_idempotency_key),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> AcceptedOperationResponse:
    # 1. Authorize workspace membership (DEVELOPER role required)
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.DEVELOPER, not_found_detail="Workspace not found"
    )

    endpoint = f"/v1/workspaces/{workspace_id}/jobs"
    payload_hash = compute_payload_hash(payload)

    # 2. Check Idempotency Record
    cached_response = await check_idempotency(
        db, workspace_id, endpoint, idempotency_key, payload_hash
    )
    if cached_response:
        return cached_response

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
    outbox_event = create_outbox_event(
        workspace_id=workspace_id,
        event_id=event_id,
        topic=OutboxTopic.JOB_SUBMITTED.value,
        payload={
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
        headers={"idempotency_key": idempotency_key},
    )
    db.add(outbox_event)

    # Prepare idempotency record
    idemp_record = create_idempotency_record(
        workspace_id=workspace_id,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        response_code=202,
        operation_id=job_id,
    )
    db.add(idemp_record)

    # Commit single atomic transaction with race condition handling
    try:
        await db.commit()
    except IntegrityError as exc:
        return await handle_idempotency_race(
            db, exc, workspace_id, endpoint, idempotency_key, payload_hash
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
    idempotency_key: str = Depends(get_idempotency_key),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> AcceptedOperationResponse:
    # Find job's workspace_id without lock to verify existence and membership
    # (scalar query avoids loading Job entity into ORM identity map before lock)
    ws_stmt = select(Job.workspace_id).where(Job.id == job_id)
    workspace_id = (await db.execute(ws_stmt)).scalar_one_or_none()
    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    # Tenant isolation: verify caller membership of job's workspace (DEVELOPER or higher) BEFORE taking row lock
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.DEVELOPER, not_found_detail="Job not found"
    )

    # Acquire pessimistic row lock (populate_existing=True ensures fresh state)
    lock_stmt = (
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = (await db.execute(lock_stmt)).scalar_one()

    endpoint = f"/v1/jobs/{job_id}/cancel"
    payload_hash = compute_payload_hash(None)

    # 1. Idempotency Check
    cached_response = await check_idempotency(
        db, workspace_id, endpoint, idempotency_key, payload_hash
    )
    if cached_response:
        return cached_response

    # Terminal jobs cannot be cancelled
    if job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot cancel job in terminal state '{job.state.value}'",
        )

    # If already CANCEL_REQUESTED, do not emit duplicate outbox event (T9)
    if job.state != JobState.CANCEL_REQUESTED:
        job.state = JobState.CANCEL_REQUESTED
        event_id = uuid.uuid4()
        outbox_event = create_outbox_event(
            workspace_id=workspace_id,
            event_id=event_id,
            topic=OutboxTopic.JOB_CANCELLATION_REQUESTED.value,
            payload={
                "event_id": str(event_id),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "workspace_id": str(workspace_id),
                "job_id": str(job.id),
            },
            headers={"idempotency_key": idempotency_key},
        )
        db.add(outbox_event)

    idemp_record = create_idempotency_record(
        workspace_id=workspace_id,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        response_code=202,
        operation_id=job.id,
    )
    db.add(idemp_record)

    try:
        await db.commit()
    except IntegrityError as exc:
        return await handle_idempotency_race(
            db, exc, workspace_id, endpoint, idempotency_key, payload_hash
        )

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
    idempotency_key: str = Depends(get_idempotency_key),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> AcceptedOperationResponse:
    # Find job's workspace_id without lock to verify existence and membership
    # (scalar query avoids loading Job entity into ORM identity map before lock)
    ws_stmt = select(Job.workspace_id).where(Job.id == job_id)
    workspace_id = (await db.execute(ws_stmt)).scalar_one_or_none()
    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    # Tenant isolation: verify caller membership of job's workspace (DEVELOPER or higher) BEFORE taking row lock
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.DEVELOPER, not_found_detail="Job not found"
    )

    # Acquire pessimistic row lock (populate_existing=True ensures fresh state)
    lock_stmt = (
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    original_job = (await db.execute(lock_stmt)).scalar_one()

    endpoint = f"/v1/jobs/{job_id}/reruns"
    payload_hash = compute_payload_hash(None)

    # 1. Idempotency Check
    cached_response = await check_idempotency(
        db, workspace_id, endpoint, idempotency_key, payload_hash
    )
    if cached_response:
        return cached_response

    if original_job.state not in (JobState.FAILED, JobState.CANCELLED, JobState.SUCCEEDED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot rerun job while active in state '{original_job.state.value}'",
        )

    # Create new logical job linked to predecessor (name guaranteed <= 100 characters)
    base_name = original_job.name[:94]
    new_job_name = f"{base_name}-rerun"

    new_job_id = uuid.uuid4()
    new_job = Job(
        id=new_job_id,
        workspace_id=workspace_id,
        name=new_job_name,
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
    outbox_event = create_outbox_event(
        workspace_id=workspace_id,
        event_id=event_id,
        topic=OutboxTopic.JOB_SUBMITTED.value,
        payload={
            "event_id": str(event_id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workspace_id": str(workspace_id),
            "job_id": str(new_job_id),
            "name": new_job.name,
            "image_digest": new_job.image_digest,
            "command_args": new_job.command_args,
            "timeout_seconds": new_job.timeout_seconds,
            "max_retries": new_job.max_retries,
            "parent_job_id": str(original_job.id),
        },
        headers={"rerun_from": str(original_job.id), "idempotency_key": idempotency_key},
    )
    db.add(outbox_event)

    idemp_record = create_idempotency_record(
        workspace_id=workspace_id,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        response_code=202,
        operation_id=new_job_id,
    )
    db.add(idemp_record)

    try:
        await db.commit()
    except IntegrityError as exc:
        return await handle_idempotency_race(
            db, exc, workspace_id, endpoint, idempotency_key, payload_hash
        )

    return AcceptedOperationResponse(
        operation_id=new_job_id,
        status="ACCEPTED",
        status_url=f"/v1/operations/{new_job_id}",
    )


@router.get(
    "/workspaces/{workspace_id}/jobs",
    response_model=JobListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_workspace_jobs(
    workspace_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> JobListResponse:
    # 1. Authorize workspace membership (VIEWER or higher)
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Workspace not found"
    )

    # 2. Query jobs with cursor pagination
    stmt = (
        select(Job)
        .options(selectinload(Job.attempts))
        .where(Job.workspace_id == workspace_id)
    )
    if cursor:
        cursor_ts, cursor_id = decode_cursor(cursor)
        stmt = stmt.where(
            (Job.created_at < cursor_ts)
            | ((Job.created_at == cursor_ts) & (Job.id < cursor_id))
        )

    stmt = stmt.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit + 1)
    jobs = (await db.execute(stmt)).scalars().all()

    next_cursor = None
    if len(jobs) > limit:
        next_item = jobs[limit - 1]
        next_cursor = encode_cursor(next_item.created_at, next_item.id)
        jobs = jobs[:limit]

    items = [
        JobDetailsResponse(
            id=j.id,
            workspace_id=j.workspace_id,
            name=j.name,
            state=j.state,
            current_attempt_number=j.current_attempt_number,
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
                for att in j.attempts
            ],
            created_at=j.created_at,
        )
        for j in jobs
    ]
    return JobListResponse(items=items, next_cursor=next_cursor)
