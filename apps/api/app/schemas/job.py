import uuid
from datetime import datetime
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

from app.models.job import JobState


class SubmitJobRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    image_digest: str = Field(..., min_length=5)
    command_args: List[str] = Field(default_factory=list)
    env_vars: Dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=600, ge=10, le=3600)
    max_retries: int = Field(default=3, ge=0, le=5)


class JobAttemptItem(BaseModel):
    attempt_number: int
    state: JobState
    resource_uid: Optional[str] = None
    lease_epoch: int
    exit_code: Optional[int] = None
    failure_reason: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class JobDetailsResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    state: JobState
    current_attempt_number: int
    attempts: List[JobAttemptItem]
    created_at: datetime
