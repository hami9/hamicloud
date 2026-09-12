import uuid
from datetime import datetime
from typing import Dict, Optional
from pydantic import BaseModel, Field

from app.models.application import WorkloadType


class CreateApplicationRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=3, max_length=100, pattern="^[a-z0-9-]+$")
    workload_type: WorkloadType = WorkloadType.HTTP_SERVICE


class ApplicationResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    slug: str
    workload_type: str
    desired_generation: int
    current_release_id: Optional[uuid.UUID] = None
    created_at: datetime


class DeployReleaseRequest(BaseModel):
    image_digest: str = Field(..., min_length=5)
    port: int = Field(default=8080, ge=1, le=65535)
    health_path: str = Field(default="/healthz")
    env_vars: Dict[str, str] = Field(default_factory=dict)
    cpu_limit: str = Field(default="500m")
    memory_limit: str = Field(default="512Mi")


class RollbackRequest(BaseModel):
    target_release_id: uuid.UUID
