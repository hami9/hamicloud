import enum
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


class ErrorCode(str, enum.Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    BAD_REQUEST = "BAD_REQUEST"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    INTERNAL_SERVER_ERROR = "INTERNAL_SERVER_ERROR"


class ErrorResponse(BaseModel):
    error_code: ErrorCode
    message: str
    correlation_id: str
    details: Optional[Dict[str, Any]] = None


class AcceptedOperationResponse(BaseModel):
    operation_id: uuid.UUID
    status: str = "ACCEPTED"
    status_url: str


class OperationKind(str, enum.Enum):
    RELEASE = "RELEASE"
    JOB = "JOB"


class OperationStatus(str, enum.Enum):
    ACCEPTED = "ACCEPTED"
    QUEUED = "QUEUED"
    ADMITTED = "ADMITTED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REQUESTED = "REQUESTED"
    BUILDING = "BUILDING"
    IMAGE_READY = "IMAGE_READY"
    DEPLOYING = "DEPLOYING"
    HEALTHY = "HEALTHY"
    BUILD_FAILED = "BUILD_FAILED"
    DEPLOY_FAILED = "DEPLOY_FAILED"


class OperationStatusResponse(BaseModel):
    operation_id: uuid.UUID
    operation_kind: OperationKind
    status: OperationStatus
    status_url: str
    created_at: Optional[datetime] = None
    details: Optional[Dict[str, Any]] = None
