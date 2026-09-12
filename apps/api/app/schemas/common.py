import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    timestamp: datetime


class ReadinessResponse(BaseModel):
    ready: bool
    database: bool
    redis: bool
    timestamp: datetime


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    correlation_id: str
    details: Optional[Dict[str, Any]] = None


class AcceptedOperationResponse(BaseModel):
    operation_id: uuid.UUID
    status: str = "ACCEPTED"
    status_url: str
