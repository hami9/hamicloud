import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.router import api_v1_router
from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.schemas.common import ErrorResponse, HealthResponse, ReadinessResponse


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup validation
    yield
    # Graceful shutdown cleanup
    await engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    docs_url=f"{settings.API_V1_PREFIX}/docs",
    redoc_url=f"{settings.API_V1_PREFIX}/redoc",
    lifespan=lifespan,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Correlation ID Middleware
@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next) -> Response:
    correlation_id = request.headers.get("X-Correlation-ID") or f"req_{uuid.uuid4().hex}"
    request.state.correlation_id = correlation_id
    response: Response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response


# Global Exception Handler
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", "unknown")
    error_code = "HTTP_ERROR"
    if exc.status_code == 404:
        error_code = "NOT_FOUND"
    elif exc.status_code == 409:
        error_code = "CONFLICT"
    elif exc.status_code == 429:
        error_code = "RATE_LIMITED"
    elif exc.status_code == 400:
        error_code = "BAD_REQUEST"
    elif exc.status_code == 401:
        error_code = "UNAUTHORIZED"
    elif exc.status_code == 403:
        error_code = "FORBIDDEN"
    elif exc.status_code == 503:
        error_code = "SERVICE_UNAVAILABLE"
    elif exc.status_code == 501:
        error_code = "NOT_IMPLEMENTED"

    message = str(exc.detail)
    details = None
    if isinstance(exc.detail, dict):
        message = str(exc.detail.get("message", "An error occurred"))
        details = {k: v for k, v in exc.detail.items() if k != "message"}

    resp_headers = {"X-Correlation-ID": correlation_id}
    if exc.headers:
        resp_headers.update(exc.headers)

    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error_code=error_code,
            message=message,
            correlation_id=correlation_id,
            details=details,
        ).model_dump(exclude_none=True),
        headers=resp_headers,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", "unknown")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            error_code="INTERNAL_SERVER_ERROR",
            message="An unexpected internal platform error occurred.",
            correlation_id=correlation_id,
        ).model_dump(),
        headers={"X-Correlation-ID": correlation_id},
    )


# Probes
@app.get("/healthz", response_model=HealthResponse, tags=["Observability"])
async def healthz() -> HealthResponse:
    """Liveness probe: verifies the HTTP control API is running."""
    return HealthResponse(
        status="ok",
        version=settings.VERSION,
        timestamp=datetime.now(timezone.utc),
    )


@app.get("/readyz", response_model=ReadinessResponse, tags=["Observability"])
async def readyz() -> ReadinessResponse:
    """Readiness probe: verifies database and redis connectivity."""
    db_ok = False
    try:
        async with AsyncSessionLocal() as session:
            res = await session.execute(text("SELECT 1"))
            db_ok = res.scalar() == 1
    except Exception:
        db_ok = False

    redis_ok = False
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        redis_ok = await r.ping()
        await r.aclose()
    except Exception:
        redis_ok = False

    ready = db_ok and redis_ok
    if not ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Platform dependencies are not ready",
                "database": db_ok,
                "redis": redis_ok,
            },
        )

    return ReadinessResponse(
        ready=True,
        database=db_ok,
        redis=redis_ok,
        timestamp=datetime.now(timezone.utc),
    )


# Mount API v1
app.include_router(api_v1_router, prefix=settings.API_V1_PREFIX)
