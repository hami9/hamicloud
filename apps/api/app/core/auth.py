from dataclasses import dataclass
import logging
from typing import Any, Optional
import uuid
from fastapi import Header, HTTPException, status
import jwt
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.workspace import WorkspaceMembership, WorkspaceRole

logger = logging.getLogger(__name__)

# Cached JWKS client singleton (thread-safe, handles in-memory key caching)
_jwks_client: Optional[PyJWKClient] = None


def get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(
            settings.effective_jwks_url,
            cache_jwk_set=True,
            lifespan=settings.OIDC_JWKS_CACHE_TTL,
        )
    return _jwks_client


def set_jwks_client(client: Optional[PyJWKClient]) -> None:
    """Allow injecting custom or mock PyJWKClient for testing."""
    global _jwks_client
    _jwks_client = client


@dataclass(frozen=True)
class Caller:
    """Authenticated caller identity."""
    subject: str
    email: Optional[str] = None
    username: Optional[str] = None


def decode_oidc_token(token: str) -> dict[str, Any]:
    """Validate and decode an OIDC JWT Bearer token using JWKS public keys."""
    jwks_client = get_jwks_client()
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        decode_kwargs: dict[str, Any] = {
            "algorithms": settings.OIDC_ALGORITHMS,
            "issuer": settings.OIDC_ISSUER_URL,
        }
        if settings.OIDC_AUDIENCE:
            decode_kwargs["audience"] = settings.OIDC_AUDIENCE
        else:
            decode_kwargs["options"] = {"verify_aud": False}

        payload: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            **decode_kwargs,
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="The access token expired"'},
        )
    except jwt.InvalidIssuerError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token issuer",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="Invalid issuer"'},
        )
    except jwt.InvalidAudienceError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token audience",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="Invalid audience"'},
        )
    except jwt.PyJWTError as e:
        logger.warning("Invalid JWT token: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="Invalid access token"'},
        )
    except Exception as e:
        logger.error("Error fetching JWKS or verifying token: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to verify token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_caller(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_dev_subject: Optional[str] = Header(None, alias="X-Dev-Subject"),
) -> Caller:
    """Establish caller identity via OIDC Bearer token or development seam.

    In production/staging: Requires Authorization: Bearer <token>.
    In development: Accepts Bearer token, or falls back to X-Dev-Subject seam.
    """
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
            token = parts[1].strip()
            payload = decode_oidc_token(token)
            sub = payload.get("sub")
            if not sub:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token missing subject claim",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return Caller(
                subject=str(sub),
                email=payload.get("email"),
                username=payload.get("preferred_username"),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header format",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # Fallback to dev seam ONLY if in development and X-Dev-Subject provided
    subject = x_dev_subject.strip() if x_dev_subject else None
    if settings.ENVIRONMENT == "development" and subject:
        return Caller(subject=subject)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


ROLE_RANK = {
    WorkspaceRole.VIEWER: 1,
    WorkspaceRole.DEVELOPER: 2,
    WorkspaceRole.OWNER: 3,
}


async def authorize_workspace_access(
    db: AsyncSession,
    caller: Caller,
    workspace_id: uuid.UUID,
    min_role: WorkspaceRole = WorkspaceRole.VIEWER,
    not_found_detail: str = "Workspace not found",
) -> WorkspaceMembership:
    """Authorize caller access to a workspace based on workspace_memberships.

    Returns the WorkspaceMembership if caller is a member with at least min_role.
    If caller is NOT a member, raises 404 with not_found_detail (preventing cross-tenant leakage).
    If caller IS a member but has a lower role than min_role, raises 403 Forbidden.
    """
    stmt = select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_subject == caller.subject,
    )
    result = await db.execute(stmt)
    membership = result.scalar_one_or_none()

    if not membership:
        # Non-member receives 404 byte-identical to resource not found
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=not_found_detail,
        )

    if ROLE_RANK.get(membership.role, 0) < ROLE_RANK.get(min_role, 0):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient role for this operation",
        )

    return membership
