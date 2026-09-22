import enum
from typing import Any, Dict, Optional, Union
import uuid

from app.models.outbox import OutboxEvent, OutboxStatus


class OutboxTopic(str, enum.Enum):
    JOB_SUBMITTED = "job.submitted.v1"
    JOB_CANCELLATION_REQUESTED = "job.cancellation.requested.v1"
    APP_DEPLOYMENT_REQUESTED = "app.deployment.requested.v1"
    JOB_ATTEMPT_FAILED = "job.attempt.failed.v1"
    JOB_ATTEMPT_SUCCEEDED = "job.attempt.succeeded.v1"
    WORKLOAD_RECONCILIATION_REQUESTED = "workload.reconciliation.requested.v1"


def create_outbox_event(
    *,
    workspace_id: uuid.UUID,
    topic: Union[OutboxTopic, str],
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    schema_version: int = 1,
    event_id: Optional[uuid.UUID] = None,
) -> OutboxEvent:
    """Central outbox event helper ensuring schema_version and workspace_id are set at every producer (T16)."""
    topic_str = topic.value if hasattr(topic, "value") else str(topic)
    model = OutboxEvent
    evt = model(
        id=uuid.uuid4(),
        event_id=event_id if event_id is not None else uuid.uuid4(),
        workspace_id=workspace_id,
        schema_version=schema_version,
        payload_json=payload,
        headers_json=headers if headers is not None else {},
        status=OutboxStatus.PENDING,
        retry_count=0,
        published_at=None,
    )
    evt.topic = topic_str
    return evt



