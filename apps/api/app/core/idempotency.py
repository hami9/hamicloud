import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency import IdempotencyRecord
from app.schemas.common import AcceptedOperationResponse


def compute_payload_hash(payload: Any) -> str:
    """Compute deterministic SHA-256 hash for a request payload."""
    if payload is None:
        payload_dict: Dict[str, Any] = {}
    elif isinstance(payload, BaseModel):
        payload_dict = payload.model_dump(mode="json")
    elif isinstance(payload, dict):
        payload_dict = payload
    else:
        payload_dict = {"value": str(payload)}

    encoded = json.dumps(payload_dict, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def check_idempotency(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    endpoint: str,
    idempotency_key: str,
    payload_hash: str,
) -> Optional[AcceptedOperationResponse]:
    """Check for an existing unexpired idempotency record.

    Retained for at least 24 hours (T10). If an unexpired record exists:
      - If request_hash matches: returns cached AcceptedOperationResponse (replay).
      - If request_hash differs: raises 409 Conflict with error_code IDEMPOTENCY_CONFLICT.
    If record does not exist or has expired, returns None.
    """
    now = datetime.now(timezone.utc)
    stmt = select(IdempotencyRecord).where(
        IdempotencyRecord.workspace_id == workspace_id,
        IdempotencyRecord.endpoint == endpoint,
        IdempotencyRecord.idempotency_key == idempotency_key,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if not existing:
        return None

    if existing.expires_at <= now:
        # Expired! Purge it so the key can be reused as new (T10)
        await db.delete(existing)
        await db.flush()
        return None

    if existing.request_hash == payload_hash:
        return AcceptedOperationResponse(
            operation_id=uuid.UUID(existing.response_body["operation_id"]),
            status=existing.response_body["status"],
            status_url=existing.response_body["status_url"],
        )

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": "Idempotency key reused with different request payload",
            "error_code": "IDEMPOTENCY_CONFLICT",
        },
    )


def create_idempotency_record(
    workspace_id: uuid.UUID,
    endpoint: str,
    idempotency_key: str,
    payload_hash: str,
    response_code: int,
    operation_id: uuid.UUID,
    status_str: str = "ACCEPTED",
    ttl_hours: int = 24,
) -> IdempotencyRecord:
    """Create a new IdempotencyRecord entity with 24-hour expiration."""
    response_data = {
        "operation_id": str(operation_id),
        "status": status_str,
        "status_url": f"/v1/operations/{operation_id}",
    }
    return IdempotencyRecord(
        workspace_id=workspace_id,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=payload_hash,
        response_code=response_code,
        response_body=response_data,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
    )


def is_idempotency_violation(exc: IntegrityError) -> bool:
    """Detect if an IntegrityError was caused specifically by uq_idempotency_workspace_key."""
    orig = getattr(exc, "orig", None)
    cause = getattr(orig, "__cause__", None)
    constraint = getattr(cause, "constraint_name", None) or getattr(orig, "constraint_name", None)
    return constraint == "uq_idempotency_workspace_key"


async def handle_idempotency_race(
    db: AsyncSession,
    exc: IntegrityError,
    workspace_id: uuid.UUID,
    endpoint: str,
    idempotency_key: str,
    payload_hash: str,
) -> AcceptedOperationResponse:
    """Handle race condition when concurrent requests insert identical idempotency key.

    Replays the stored response only when the violated constraint is
    uq_idempotency_workspace_key; re-raises anything else as a 500 error (T11).
    """
    await db.rollback()

    if not is_idempotency_violation(exc):
        # Non-idempotency integrity errors must re-raise as 500 internal server error
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database integrity constraint violation",
        )

    # Re-query the concurrent idempotency record
    stmt = select(IdempotencyRecord).where(
        IdempotencyRecord.workspace_id == workspace_id,
        IdempotencyRecord.endpoint == endpoint,
        IdempotencyRecord.idempotency_key == idempotency_key,
    )
    concurrent_idemp = (await db.execute(stmt)).scalar_one_or_none()
    if concurrent_idemp and concurrent_idemp.request_hash == payload_hash:
        return AcceptedOperationResponse(
            operation_id=uuid.UUID(concurrent_idemp.response_body["operation_id"]),
            status=concurrent_idemp.response_body["status"],
            status_url=concurrent_idemp.response_body["status_url"],
        )

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": "Idempotency key reused with different request payload",
            "error_code": "IDEMPOTENCY_CONFLICT",
        },
    )
