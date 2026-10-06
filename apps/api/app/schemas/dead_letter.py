import uuid
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel


class DeadLetterRecordItem(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    workspace_id: uuid.UUID
    job_name: str
    last_attempt: int
    exit_code: Optional[int] = None
    failure_reason: Optional[str] = None
    created_at: datetime


class DeadLetterRecordListResponse(BaseModel):
    items: List[DeadLetterRecordItem]
    next_cursor: Optional[str] = None
