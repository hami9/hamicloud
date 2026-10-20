import time
import uuid
from fastapi import HTTPException, Request, status

from app.core.config import settings


async def check_workspace_rate_limit(
    request: Request,
    workspace_id: uuid.UUID,
    route_class: str = "write",
) -> None:
    """Enforce per-workspace token-bucket / fixed-window rate limits backed by Redis."""
    limit = settings.RATE_LIMIT_REQUESTS_PER_MINUTE
    if limit <= 0:
        return

    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        if settings.REDIS_FAIL_CLOSED or settings.ENVIRONMENT != "development":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiter store unavailable",
            )
        return

    bucket = int(time.time() // 60)
    key = f"hc:ratelimit:{workspace_id}:{route_class}:{bucket}"

    try:
        current = int(await redis_client.incr(key))
        if current == 1:
            await redis_client.expire(key, 60)
    except Exception as exc:
        if settings.REDIS_FAIL_CLOSED or settings.ENVIRONMENT != "development":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiter store unavailable",
            ) from exc
        return

    if current > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error_code": "RATE_LIMITED",
                "message": f"Workspace rate limit exceeded ({limit} requests/minute)",
                "limit": limit,
            },
            headers={
                "Retry-After": "60",
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
            },
        )
