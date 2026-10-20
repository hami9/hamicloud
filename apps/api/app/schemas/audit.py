import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class AuditEventItem(BaseModel):
    id: uuid.UUID
    workspace_id: Optional[uuid.UUID] = None
    actor_subject: str
    action: str
    target_type: str
    target_id: str
    result: str
    details_json: Dict[str, Any]
    created_at: datetime


class AuditEventListResponse(BaseModel):
    items: List[AuditEventItem]
    next_cursor: Optional[str] = None
