from datetime import datetime
import uuid
from pydantic import BaseModel, ConfigDict


class WorkspaceTelemetryResponse(BaseModel):
    workspace_id: uuid.UUID
    requests_total: int
    status_2xx: int
    status_4xx: int
    status_5xx: int
    error_rate_5xx: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    outbox_pending_total: int
    active_leases_total: int
    queued_jobs_total: int
    oldest_queued_job_age_seconds: float
    running_jobs_total: int
    healthy_services_total: int
    job_retry_ratio: float
    dead_letter_total: int
    collected_at: datetime

    model_config = ConfigDict(from_attributes=True)
