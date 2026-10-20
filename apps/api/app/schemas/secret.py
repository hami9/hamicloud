import uuid
from datetime import datetime
from typing import List
from pydantic import BaseModel, Field


class UpsertSecretRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, pattern="^[A-Z0-9_]+$")
    value: str = Field(..., min_length=1, max_length=16384)


class SecretMetadataResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    key_version: int
    created_at: datetime
    updated_at: datetime


class SecretListResponse(BaseModel):
    items: List[SecretMetadataResponse]


class DeleteSecretResponse(BaseModel):
    status: str = "DELETED"
    name: str
