import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import redis.asyncio as aioredis
from sqlalchemy import text

from app.api.v1.router import api_v1_router
from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.schemas.common import ErrorCode, ErrorResponse, HealthResponse, ReadinessResponse


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Initialize shared Redis client pool for probes and caching (T15)
    app.state.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    yield
    # Graceful shutdown cleanup
    if hasattr(app.state, "redis") and app.state.redis is not None:
        await app.state.redis.aclose()
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


# Request Validation Error Handler (T12)
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", "unknown")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            error_code=ErrorCode.VALIDATION_ERROR,
            message="Request validation failed",
            correlation_id=correlation_id,
            details={"errors": jsonable_encoder(exc.errors())},
        ).model_dump(exclude_none=True),
        headers={"X-Correlation-ID": correlation_id},
    )


# Global HTTP Exception Handler
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", "unknown")

    message = str(exc.detail)
    details = None
    custom_error_code = None

    if isinstance(exc.detail, dict):
        message = str(exc.detail.get("message", "An error occurred"))
        custom_error_code = exc.detail.get("error_code")
        details = {k: v for k, v in exc.detail.items() if k not in ("message", "error_code")}
        if not details:
            details = None

    if custom_error_code and custom_error_code in ErrorCode.__members__:
        error_code = ErrorCode(custom_error_code)
    elif exc.status_code == 400:
        error_code = ErrorCode.BAD_REQUEST
    elif exc.status_code == 401:
        error_code = ErrorCode.UNAUTHORIZED
    elif exc.status_code == 403:
        error_code = ErrorCode.FORBIDDEN
    elif exc.status_code == 404:
        error_code = ErrorCode.NOT_FOUND
    elif exc.status_code == 409:
        error_code = ErrorCode.CONFLICT
    elif exc.status_code == 422:
        error_code = ErrorCode.VALIDATION_ERROR
    elif exc.status_code == 429:
        error_code = ErrorCode.RATE_LIMITED
    elif exc.status_code == 501:
        error_code = ErrorCode.NOT_IMPLEMENTED
    elif exc.status_code == 503:
        error_code = ErrorCode.SERVICE_UNAVAILABLE
    else:
        error_code = ErrorCode.INTERNAL_SERVER_ERROR

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
            error_code=ErrorCode.INTERNAL_SERVER_ERROR,
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
async def readyz(request: Request) -> ReadinessResponse:
    """Readiness probe: verifies database and redis connectivity using app lifespan pool (T15)."""
    db_ok = False
    try:
        async with AsyncSessionLocal() as session:
            res = await session.execute(text("SELECT 1"))
            db_ok = res.scalar() == 1
    except Exception:
        db_ok = False

    redis_ok = False
    try:
        r = getattr(request.app.state, "redis", None)
        if r is not None:
            redis_ok = bool(await r.ping())
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
