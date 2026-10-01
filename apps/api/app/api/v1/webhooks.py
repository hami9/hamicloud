import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import OutboxTopic, create_outbox_event
from app.db.session import get_db
from app.models.application import Application
from app.models.release import Release, ReleaseStatus
from app.models.repository import Repository
from app.models.webhook_delivery import WebhookDelivery
from app.schemas.repository import WebhookDeliveryResponse

router = APIRouter(tags=["Webhooks"])


def verify_github_signature(raw_body: bytes, secret: str, signature_header: Optional[str]) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    mac = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256)
    expected_sig = "sha256=" + mac.hexdigest()
    return hmac.compare_digest(signature_header, expected_sig)


@router.post(
    "/webhooks/github/{repository_id}",
    response_model=WebhookDeliveryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def handle_github_webhook(
    repository_id: uuid.UUID,
    request: Request,
    response: Response,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
    x_github_delivery: Optional[str] = Header(None, alias="X-GitHub-Delivery"),
    x_github_event: Optional[str] = Header("push", alias="X-GitHub-Event"),
    db: AsyncSession = Depends(get_db),
) -> WebhookDeliveryResponse:
    # 1. Fetch target repository
    repo = (
        await db.execute(select(Repository).where(Repository.id == repository_id))
    ).scalar_one_or_none()
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    # 2. Read raw request bytes and verify HMAC-SHA256 signature
    raw_body = await request.body()
    if not verify_github_signature(raw_body, repo.webhook_secret, x_hub_signature_256):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Hub-Signature-256 header",
        )

    # 3. Verify delivery ID presence
    if not x_github_delivery:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-GitHub-Delivery header",
        )

    payload_hash = hashlib.sha256(raw_body).hexdigest()

    # 4. Check for duplicate webhook delivery (M3 Invariant: duplicate webhook does not duplicate a build)
    existing_delivery = (
        await db.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.repository_id == repository_id,
                WebhookDelivery.delivery_id == x_github_delivery,
            )
        )
    ).scalar_one_or_none()
    if existing_delivery:
        response.status_code = status.HTTP_200_OK
        return WebhookDeliveryResponse(
            delivery_id=x_github_delivery,
            status="DUPLICATE",
            event_type=existing_delivery.event_type,
            processed_at=existing_delivery.created_at,
        )

    # 5. Handle ping event
    if x_github_event == "ping":
        delivery = WebhookDelivery(
            id=uuid.uuid4(),
            workspace_id=repo.workspace_id,
            repository_id=repo.id,
            delivery_id=x_github_delivery,
            event_type="ping",
            payload_hash=payload_hash,
            status="PING_ACKNOWLEDGED",
        )
        db.add(delivery)
        await db.commit()
        response.status_code = status.HTTP_200_OK
        return WebhookDeliveryResponse(
            delivery_id=x_github_delivery,
            status="PING_ACKNOWLEDGED",
            event_type="ping",
            processed_at=delivery.created_at,
        )

    # Only X-GitHub-Event "push" triggers build workflows; ignore all other events cleanly
    if x_github_event != "push":
        delivery = WebhookDelivery(
            id=uuid.uuid4(),
            workspace_id=repo.workspace_id,
            repository_id=repo.id,
            delivery_id=x_github_delivery,
            event_type=x_github_event or "unknown",
            payload_hash=payload_hash,
            status="IGNORED_NON_PUSH_EVENT",
        )
        db.add(delivery)
        await db.commit()
        response.status_code = status.HTTP_200_OK
        return WebhookDeliveryResponse(
            delivery_id=x_github_delivery,
            status="IGNORED_NON_PUSH_EVENT",
            event_type=x_github_event or "unknown",
            processed_at=delivery.created_at,
        )

    # 6. Parse push event
    try:
        payload: Dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON payload: {exc}",
        ) from exc

    commit_sha = payload.get("after")
    ref = payload.get("ref", "")
    head_commit = payload.get("head_commit") or {}
    commit_msg = head_commit.get("message")

    # If branch was deleted, ignore cleanly
    if not commit_sha or commit_sha == "0000000000000000000000000000000000000000":
        delivery = WebhookDelivery(
            id=uuid.uuid4(),
            workspace_id=repo.workspace_id,
            repository_id=repo.id,
            delivery_id=x_github_delivery,
            event_type=x_github_event or "push",
            payload_hash=payload_hash,
            status="IGNORED_BRANCH_DELETED",
        )
        db.add(delivery)
        await db.commit()
        response.status_code = status.HTTP_200_OK
        return WebhookDeliveryResponse(
            delivery_id=x_github_delivery,
            status="IGNORED_BRANCH_DELETED",
            event_type=x_github_event or "push",
            processed_at=delivery.created_at,
        )

    branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ref

    # 7. Find applications linked to this repository and branch
    apps_stmt = select(Application).where(
        Application.repository_id == repository_id,
        (Application.git_branch == branch) | (Application.git_branch.is_(None)),
    )
    matched_apps = list((await db.execute(apps_stmt)).scalars().all())

    last_release_id = None
    if not matched_apps:
        # Record delivery even if no app matches branch
        delivery = WebhookDelivery(
            id=uuid.uuid4(),
            workspace_id=repo.workspace_id,
            repository_id=repo.id,
            delivery_id=x_github_delivery,
            event_type=x_github_event or "push",
            payload_hash=payload_hash,
            status="IGNORED_NO_MATCHING_APP",
        )
        db.add(delivery)
        await db.commit()
        response.status_code = status.HTTP_200_OK
        return WebhookDeliveryResponse(
            delivery_id=x_github_delivery,
            status="IGNORED_NO_MATCHING_APP",
            event_type=x_github_event or "push",
            processed_at=delivery.created_at,
            commit_sha=commit_sha,
        )

    for app in matched_apps:
        # Determine next release number
        max_num_stmt = select(func.max(Release.release_number)).where(
            Release.application_id == app.id
        )
        max_num = (await db.execute(max_num_stmt)).scalar() or 0
        next_number = max_num + 1

        # Fetch latest release config for default port / health / env_vars
        latest_rel_stmt = (
            select(Release)
            .where(Release.application_id == app.id)
            .order_by(Release.release_number.desc())
            .limit(1)
        )
        latest_rel = (await db.execute(latest_rel_stmt)).scalar_one_or_none()
        config_json = latest_rel.config_json if latest_rel else {"port": 8080, "health_path": "/healthz"}

        release = Release(
            id=uuid.uuid4(),
            application_id=app.id,
            workspace_id=app.workspace_id,
            repository_id=repo.id,
            release_number=next_number,
            commit_sha=commit_sha,
            git_ref=branch,
            commit_message=commit_msg[:250] if commit_msg else None,
            image_digest="pending",
            config_json=config_json,
            status=ReleaseStatus.REQUESTED,
        )
        db.add(release)
        last_release_id = release.id

        # Write transactional outbox event
        outbox_evt = create_outbox_event(
            workspace_id=app.workspace_id,
            topic=OutboxTopic.APP_BUILD_REQUESTED,
            payload={
                "release_id": str(release.id),
                "application_id": str(app.id),
                "workspace_id": str(app.workspace_id),
                "repository_id": str(repo.id),
                "repo_url": repo.repo_url,
                "commit_sha": commit_sha,
                "git_ref": branch,
                "dockerfile_path": app.dockerfile_path,
                "context_dir": app.context_dir,
            },
        )
        db.add(outbox_evt)

    # Record delivery
    delivery = WebhookDelivery(
        id=uuid.uuid4(),
        workspace_id=repo.workspace_id,
        repository_id=repo.id,
        delivery_id=x_github_delivery,
        event_type=x_github_event or "push",
        payload_hash=payload_hash,
        status="PROCESSED",
    )
    db.add(delivery)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # In case of concurrent delivery race on uq_webhook_deliveries_repo_delivery
        response.status_code = status.HTTP_200_OK
        return WebhookDeliveryResponse(
            delivery_id=x_github_delivery,
            status="DUPLICATE",
            event_type=x_github_event or "push",
            processed_at=datetime.now(timezone.utc),
        )

    return WebhookDeliveryResponse(
        delivery_id=x_github_delivery,
        status="PROCESSED",
        event_type=x_github_event or "push",
        processed_at=datetime.now(timezone.utc),
        release_id=last_release_id,
        commit_sha=commit_sha,
    )
