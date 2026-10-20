import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.job import Job, JobState
from app.models.quota import QuotaReservation, QuotaResourceClass, QuotaStatus
from app.models.release import Release, ReleaseStatus

ACTIVE_JOB_STATES = (
    JobState.ADMITTED,
    JobState.STARTING,
    JobState.RUNNING,
)

PENDING_JOB_STATES = (
    JobState.QUEUED,
    JobState.ADMITTED,
    JobState.STARTING,
    JobState.RUNNING,
    JobState.RETRY_WAIT,
    JobState.RECOVERY_PENDING,
)

ACTIVE_SERVICE_RELEASE_STATUSES = (
    ReleaseStatus.IMAGE_READY,
    ReleaseStatus.DEPLOYING,
    ReleaseStatus.HEALTHY,
)


async def expire_stale_reservations(db: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Mark any expired ACTIVE quota reservations as EXPIRED."""
    now = datetime.now(timezone.utc)
    await db.execute(
        update(QuotaReservation)
        .where(
            QuotaReservation.workspace_id == workspace_id,
            QuotaReservation.status == QuotaStatus.ACTIVE,
            QuotaReservation.expires_at <= now,
        )
        .values(status=QuotaStatus.EXPIRED, updated_at=now)
    )


async def get_workspace_quota_summary(
    db: AsyncSession, workspace_id: uuid.UUID
) -> Tuple[int, int, int, int, List[QuotaReservation]]:
    """Return (active_jobs, active_builds, deployed_services, pending_operations, active_reservations)."""
    await expire_stale_reservations(db, workspace_id)
    now = datetime.now(timezone.utc)

    res_stmt = (
        select(QuotaReservation)
        .where(
            QuotaReservation.workspace_id == workspace_id,
            QuotaReservation.status == QuotaStatus.ACTIVE,
            QuotaReservation.expires_at > now,
        )
        .order_by(QuotaReservation.created_at.desc())
    )
    active_reservations = list((await db.execute(res_stmt)).scalars().all())

    res_units = {
        QuotaResourceClass.CONCURRENT_JOB: 0,
        QuotaResourceClass.CONCURRENT_BUILD: 0,
        QuotaResourceClass.DEPLOYED_SERVICE: 0,
    }
    for r in active_reservations:
        res_units[r.resource_class] = res_units.get(r.resource_class, 0) + r.units

    running_jobs_count = (
        await db.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.workspace_id == workspace_id, Job.state.in_(ACTIVE_JOB_STATES))
        )
    ).scalar() or 0

    pending_jobs_count = (
        await db.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.workspace_id == workspace_id, Job.state.in_(PENDING_JOB_STATES))
        )
    ).scalar() or 0

    active_builds_count = (
        await db.execute(
            select(func.count())
            .select_from(Release)
            .where(
                Release.workspace_id == workspace_id,
                Release.repository_id.is_not(None),
                Release.status.in_((ReleaseStatus.REQUESTED, ReleaseStatus.BUILDING)),
            )
        )
    ).scalar() or 0

    deployed_apps_count = (
        await db.execute(
            select(func.count(func.distinct(Release.application_id)))
            .where(
                Release.workspace_id == workspace_id,
                Release.status.in_(ACTIVE_SERVICE_RELEASE_STATUSES),
            )
        )
    ).scalar() or 0

    effective_jobs = max(int(running_jobs_count), res_units[QuotaResourceClass.CONCURRENT_JOB])
    effective_builds = int(active_builds_count)
    effective_services = max(int(deployed_apps_count), res_units[QuotaResourceClass.DEPLOYED_SERVICE])
    effective_pending = int(pending_jobs_count) + int(active_builds_count)

    return (
        effective_jobs,
        effective_builds,
        effective_services,
        effective_pending,
        active_reservations,
    )


async def enforce_workspace_quota(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    resource_class: QuotaResourceClass,
    application_id: Optional[uuid.UUID] = None,
    reserve: bool = True,
) -> Optional[QuotaReservation]:
    """Verify workspace quota limits and optionally stage an ACTIVE QuotaReservation."""
    (
        effective_jobs,
        effective_builds,
        effective_services,
        effective_pending,
        _,
    ) = await get_workspace_quota_usage_for_admission(db, workspace_id, application_id)

    if effective_pending >= settings.DEFAULT_WORKSPACE_PENDING_OPERATIONS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error_code": "QUOTA_EXCEEDED",
                "message": (
                    f"Workspace quota exceeded for PENDING_OPERATIONS "
                    f"(limit={settings.DEFAULT_WORKSPACE_PENDING_OPERATIONS})"
                ),
                "resource_class": "PENDING_OPERATIONS",
                "limit": settings.DEFAULT_WORKSPACE_PENDING_OPERATIONS,
            },
            headers={"Retry-After": "30"},
        )

    now = datetime.now(timezone.utc)
    ttl_seconds = settings.DEFAULT_JOB_TIMEOUT_SECONDS

    if resource_class == QuotaResourceClass.CONCURRENT_JOB:
        limit = settings.DEFAULT_WORKSPACE_JOB_CONCURRENCY
        if effective_jobs >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error_code": "QUOTA_EXCEEDED",
                    "message": f"Workspace quota exceeded for CONCURRENT_JOB (limit={limit})",
                    "resource_class": QuotaResourceClass.CONCURRENT_JOB.value,
                    "limit": limit,
                },
                headers={"Retry-After": "15"},
            )
    elif resource_class == QuotaResourceClass.CONCURRENT_BUILD:
        limit = settings.DEFAULT_WORKSPACE_BUILD_CONCURRENCY
        ttl_seconds = settings.DEFAULT_BUILD_TIMEOUT_SECONDS
        if effective_builds >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error_code": "QUOTA_EXCEEDED",
                    "message": f"Workspace quota exceeded for CONCURRENT_BUILD (limit={limit})",
                    "resource_class": QuotaResourceClass.CONCURRENT_BUILD.value,
                    "limit": limit,
                },
                headers={"Retry-After": "15"},
            )
    elif resource_class == QuotaResourceClass.DEPLOYED_SERVICE:
        limit = settings.DEFAULT_WORKSPACE_SERVICE_CAP
        ttl_seconds = 86400
        if effective_services >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error_code": "QUOTA_EXCEEDED",
                    "message": f"Workspace quota exceeded for DEPLOYED_SERVICE (limit={limit})",
                    "resource_class": QuotaResourceClass.DEPLOYED_SERVICE.value,
                    "limit": limit,
                },
                headers={"Retry-After": "60"},
            )

    if not reserve:
        return None

    reservation = QuotaReservation(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        resource_class=resource_class,
        units=1,
        status=QuotaStatus.ACTIVE,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    db.add(reservation)
    return reservation


async def get_workspace_quota_usage_for_admission(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    application_id: Optional[uuid.UUID] = None,
) -> Tuple[int, int, int, int, List[QuotaReservation]]:
    (
        effective_jobs,
        effective_builds,
        effective_services,
        effective_pending,
        active_reservations,
    ) = await get_workspace_quota_summary(db, workspace_id)

    if application_id is not None:
        # Exclude the current application when checking if deploying a new release
        # would exceed the distinct deployed services cap for the workspace.
        other_deployed_apps = (
            await db.execute(
                select(func.count(func.distinct(Release.application_id)))
                .where(
                    Release.workspace_id == workspace_id,
                    Release.application_id != application_id,
                    Release.status.in_(ACTIVE_SERVICE_RELEASE_STATUSES),
                )
            )
        ).scalar() or 0
        effective_services = int(other_deployed_apps)

    return (
        effective_jobs,
        effective_builds,
        effective_services,
        effective_pending,
        active_reservations,
    )
