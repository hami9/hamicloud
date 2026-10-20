import uuid
from typing import Any, Dict, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent

_SENSITIVE_KEY_MARKERS = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "encrypted_value",
)


def sanitize_audit_details(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Ensure secret values, tokens, or credentials are never stored in audit details."""
    if not details:
        return {}
    cleaned: Dict[str, Any] = {}
    for k, v in details.items():
        key_lower = str(k).lower()
        if key_lower == "value" or any(marker in key_lower for marker in _SENSITIVE_KEY_MARKERS):
            cleaned[k] = "[REDACTED]"
        elif isinstance(v, dict):
            cleaned[k] = sanitize_audit_details(v)
        else:
            cleaned[k] = v
    return cleaned


def record_audit_event(
    db: AsyncSession,
    workspace_id: Optional[uuid.UUID],
    actor_subject: str,
    action: str,
    target_type: str,
    target_id: str,
    result: str = "SUCCESS",
    details: Optional[Dict[str, Any]] = None,
) -> AuditEvent:
    """Create and stage an immutable AuditEvent row within the active DB transaction."""
    evt = AuditEvent(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        actor_subject=actor_subject,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        result=result,
        details_json=sanitize_audit_details(details),
    )
    db.add(evt)
    return evt
