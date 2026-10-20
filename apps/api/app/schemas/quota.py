import uuid
from datetime import datetime
from typing import List
from pydantic import BaseModel

from app.models.quota import QuotaResourceClass, QuotaStatus


class QuotaReservationItem(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    resource_class: QuotaResourceClass
    units: int
    status: QuotaStatus
    expires_at: datetime
    created_at: datetime


class WorkspaceQuotaResponse(BaseModel):
    workspace_id: uuid.UUID
    concurrent_jobs_limit: int
    concurrent_jobs_used: int
    concurrent_builds_limit: int
    concurrent_builds_used: int
    deployed_services_limit: int
    deployed_services_used: int
    pending_operations_limit: int
    pending_operations_used: int
    reservations: List[QuotaReservationItem]
