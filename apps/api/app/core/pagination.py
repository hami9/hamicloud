import base64
from datetime import datetime, timezone
from typing import Tuple
import uuid
from fastapi import HTTPException, status


def encode_cursor(created_at: datetime, item_id: uuid.UUID) -> str:
    """Encode created_at timestamp and UUID into an opaque URL-safe base64 cursor."""
    payload = f"{created_at.isoformat()}|{item_id}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8")


def decode_cursor(cursor: str) -> Tuple[datetime, uuid.UUID]:
    """Decode opaque URL-safe base64 cursor back into created_at timestamp and UUID."""
    try:
        # Handle unpadded base64 if padding was stripped
        padded = cursor + "=" * ((4 - len(cursor) % 4) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        parts = raw.split("|", 1)
        if len(parts) != 2:
            raise ValueError("Malformed cursor parts")
        dt = datetime.fromisoformat(parts[0])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        if dt.year < 1970 or dt.year > 9999:
            raise ValueError("Cursor year out of supported range")
        return dt, uuid.UUID(parts[1])
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid pagination cursor",
        )
