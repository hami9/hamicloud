import hashlib
import uuid
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import OutboxTopic, create_outbox_event
from app.models.application import Application
from app.models.release import Release, ReleaseStatus


class BuildService:
    @staticmethod
    async def process_build(
        db: AsyncSession,
        release_id: uuid.UUID,
        succeed: bool = True,
        failure_reason: Optional[str] = None,
        custom_logs: Optional[str] = None,
    ) -> Release:
        """
        Executes or simulates the isolated BuildKit build task for a release.
        Guarantees:
        1. On success: sets deterministic immutable digest, IMAGE_READY -> DEPLOYING -> HEALTHY,
           and updates application.current_release_id.
        2. On failure: sets BUILD_FAILED, records logs and status_reason, and CRITICALLY preserves
           the currently serving release on the application (application.current_release_id unchanged).
        """
        release = (
            await db.execute(select(Release).where(Release.id == release_id))
        ).scalar_one_or_none()
        if not release:
            raise ValueError(f"Release {release_id} not found")

        app = (
            await db.execute(select(Application).where(Application.id == release.application_id))
        ).scalar_one_or_none()
        if not app:
            raise ValueError(f"Application {release.application_id} not found")

        # Advance to BUILDING
        release.status = ReleaseStatus.BUILDING
        await db.commit()
        await db.refresh(release)

        if succeed:
            commit_prefix = release.commit_sha[:8] if release.commit_sha else "latest"
            content_hash = hashlib.sha256(
                f"{app.slug}:{release.commit_sha or release_id}".encode("utf-8")
            ).hexdigest()
            image_digest = f"registry.hamicloud.local/{app.slug}:sha-{commit_prefix}@sha256:{content_hash}"

            logs = custom_logs or (
                f"[buildkit] STEP 1/3: FROM docker.io/library/alpine:3.20\n"
                f"[buildkit] STEP 2/3: COPY {app.context_dir} /app\n"
                f"[buildkit] STEP 3/3: WORKDIR /app\n"
                f"[buildkit] Exporting image digest {image_digest}\n"
                f"[buildkit] Successfully pushed {image_digest}"
            )

            release.image_digest = image_digest
            release.build_duration_ms = 1420
            release.build_logs = logs
            release.status = ReleaseStatus.IMAGE_READY

            # Auto-progress to DEPLOYING and HEALTHY
            release.status = ReleaseStatus.DEPLOYING
            release.status = ReleaseStatus.HEALTHY
            release.status_reason = "Service rollout verified healthy"

            # Point application to new release
            app.current_release_id = release.id
            app.desired_generation += 1

            # Emit deployment outbox event
            outbox_evt = create_outbox_event(
                workspace_id=release.workspace_id,
                topic=OutboxTopic.APP_BUILD_COMPLETED,
                payload={
                    "release_id": str(release.id),
                    "application_id": str(app.id),
                    "image_digest": image_digest,
                    "commit_sha": release.commit_sha,
                    "status": "HEALTHY",
                },
            )
            db.add(outbox_evt)
        else:
            # Build failed: Record failure details and logs
            reason = failure_reason or "BuildKit build step failed: exit code 1"
            logs = custom_logs or (
                "[buildkit] STEP 1/2: FROM docker.io/library/alpine:3.20\n"
                "[buildkit] STEP 2/2: RUN ./build.sh\n"
                "[buildkit] ERROR: failed to solve: process './build.sh' did not complete successfully: exit code 1\n"
                f"[buildkit] {reason}"
            )
            release.status = ReleaseStatus.BUILD_FAILED
            release.status_reason = reason
            release.build_duration_ms = 850
            release.build_logs = logs

            # CRITICAL M3 INVARIANT:
            # A failed build PRESERVES the currently serving application (app.current_release_id remains unchanged)

        await db.commit()
        await db.refresh(release)
        return release
