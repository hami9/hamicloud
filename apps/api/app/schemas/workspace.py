import uuid
from datetime import datetime
from typing import List
from pydantic import BaseModel, Field

from app.models.workspace import WorkspaceRole


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=3, max_length=100, pattern="^[a-z0-9-]+$")


class WorkspaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime


class UpsertWorkspaceMemberRequest(BaseModel):
    user_subject: str = Field(..., min_length=1, max_length=255)
    role: WorkspaceRole = Field(default=WorkspaceRole.DEVELOPER)


class WorkspaceMemberResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    user_subject: str
    role: WorkspaceRole
    created_at: datetime


class WorkspaceMemberListResponse(BaseModel):
    items: List[WorkspaceMemberResponse]


class RemoveWorkspaceMemberResponse(BaseModel):
    status: str = "REMOVED"
    user_subject: str
