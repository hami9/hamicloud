import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from app.models.application import WorkloadType
from app.models.release import ReleaseStatus


class CreateApplicationRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=3, max_length=100, pattern="^[a-z0-9-]+$")
    workload_type: WorkloadType = WorkloadType.HTTP_SERVICE
    repository_id: Optional[uuid.UUID] = None
    dockerfile_path: str = Field(default="Dockerfile", max_length=255)
    context_dir: str = Field(default=".", max_length=255)
    git_branch: Optional[str] = Field(default="main", max_length=100)


class UpdateApplicationRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    repository_id: Optional[uuid.UUID] = None
    dockerfile_path: Optional[str] = Field(None, max_length=255)
    context_dir: Optional[str] = Field(None, max_length=255)
    git_branch: Optional[str] = Field(None, max_length=100)


class ApplicationResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    slug: str
    workload_type: str
    desired_generation: int
    current_release_id: Optional[uuid.UUID] = None
    repository_id: Optional[uuid.UUID] = None
    dockerfile_path: str = "Dockerfile"
    context_dir: str = "."
    git_branch: Optional[str] = "main"
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


class ReleaseResponse(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    workspace_id: uuid.UUID
    release_number: int
    image_digest: str
    config_json: Dict[str, Any] = Field(default_factory=dict)
    status: ReleaseStatus
    status_reason: Optional[str] = None
    commit_sha: Optional[str] = None
    git_ref: Optional[str] = None
    commit_message: Optional[str] = None
    build_duration_ms: Optional[int] = None
    build_logs: Optional[str] = None
    created_at: datetime


class ReleaseListResponse(BaseModel):
    items: List[ReleaseResponse]
    next_cursor: Optional[str] = None


class ApplicationListResponse(BaseModel):
    items: List[ApplicationResponse]
    next_cursor: Optional[str] = None
