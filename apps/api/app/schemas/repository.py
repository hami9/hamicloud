import uuid
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class CreateRepositoryRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    repo_url: str = Field(..., min_length=5, max_length=255)
    webhook_secret: str = Field(..., min_length=8, max_length=255)
    default_branch: str = Field(default="main", min_length=1, max_length=100)


class RepositoryResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    repo_url: str
    default_branch: str
    created_at: datetime


class RepositoryListResponse(BaseModel):
    items: List[RepositoryResponse]
    next_cursor: Optional[str] = None


class TriggerBuildRequest(BaseModel):
    commit_sha: Optional[str] = Field(default=None, max_length=40)
    git_ref: str = Field(default="main", max_length=100)
    commit_message: Optional[str] = Field(default=None, max_length=255)


class WebhookDeliveryResponse(BaseModel):
    delivery_id: str
    status: str
    event_type: str
    processed_at: datetime
    release_id: Optional[uuid.UUID] = None
    commit_sha: Optional[str] = None
