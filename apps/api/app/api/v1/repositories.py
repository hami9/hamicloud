import urllib.parse
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Caller, authorize_workspace_access, get_caller
from app.core.config import settings
from app.core.db_errors import unexpected_integrity_error, violated_constraint
from app.core.pagination import decode_cursor, encode_cursor
from app.db.session import get_db
from app.models.repository import Repository
from app.models.workspace import WorkspaceRole
from app.schemas.repository import (
    CreateRepositoryRequest,
    RepositoryListResponse,
    RepositoryResponse,
)

router = APIRouter(tags=["Repositories"])


def extract_repo_host(repo_url: str) -> str:
    url = repo_url.strip()
    # Handle scp-style git@host:path or user@host:path (no '://' scheme)
    if "://" not in url and ":" in url:
        after_user = url.split("@", 1)[1] if "@" in url else url
        host_part = after_user.split(":", 1)[0]
        host_part = host_part.split("/")[0]
        return host_part.lower()

    # Handle standard URLs with schemes (https://, ssh://, etc.)
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid repository URL: unable to extract hostname",
        )
    return hostname.lower()


def validate_repo_url(repo_url: str) -> str:
    url = repo_url.strip()
    if not (url.startswith("https://") or url.startswith("git@") or url.startswith("ssh://")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid repository URL: must start with https://, git@, or ssh://",
        )
    host = extract_repo_host(url)
    approved_hosts = {h.strip().lower() for h in settings.APPROVED_REPOSITORY_HOSTS}
    if host not in approved_hosts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="REPOSITORY_POLICY_VIOLATION: Repository host is not in the approved repository allowlist",
        )
    return url


@router.post(
    "/workspaces/{workspace_id}/repositories",
    response_model=RepositoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_repository(
    workspace_id: uuid.UUID,
    payload: CreateRepositoryRequest,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> RepositoryResponse:
    # 1. Authorize workspace access FIRST (DEVELOPER required to connect repositories)
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.DEVELOPER, not_found_detail="Workspace not found"
    )

    # 2. Validate repository URL against allowlist policy (Roadmap requirement)
    clean_repo_url = validate_repo_url(payload.repo_url)

    # Check for name uniqueness within workspace
    stmt = select(Repository).where(
        Repository.workspace_id == workspace_id,
        Repository.name == payload.name,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Repository with name '{payload.name}' already exists in this workspace",
        )

    repo = Repository(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=payload.name,
        repo_url=clean_repo_url,
        webhook_secret=payload.webhook_secret,
        default_branch=payload.default_branch,
    )
    db.add(repo)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if violated_constraint(exc) == "uq_repository_workspace_name":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Repository with name '{payload.name}' already exists in this workspace",
            ) from exc
        raise unexpected_integrity_error(exc, "create_repository") from exc

    await db.refresh(repo)
    return RepositoryResponse(
        id=repo.id,
        workspace_id=repo.workspace_id,
        name=repo.name,
        repo_url=repo.repo_url,
        default_branch=repo.default_branch,
        created_at=repo.created_at,
    )


@router.get(
    "/workspaces/{workspace_id}/repositories",
    response_model=RepositoryListResponse,
)
async def list_repositories(
    workspace_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=100),
    cursor: Optional[str] = Query(None),
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> RepositoryListResponse:
    await authorize_workspace_access(
        db, caller, workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Workspace not found"
    )

    query = (
        select(Repository)
        .where(Repository.workspace_id == workspace_id)
        .order_by(Repository.created_at.desc(), Repository.id.desc())
    )

    if cursor:
        cursor_dt, cursor_id = decode_cursor(cursor)
        query = query.where(
            (Repository.created_at < cursor_dt)
            | ((Repository.created_at == cursor_dt) & (Repository.id < cursor_id))
        )

    query = query.limit(limit + 1)
    result = await db.execute(query)
    repos = list(result.scalars().all())

    next_cursor = None
    if len(repos) > limit:
        last = repos[limit - 1]
        next_cursor = encode_cursor(last.created_at, last.id)
        repos = repos[:limit]

    items = [
        RepositoryResponse(
            id=r.id,
            workspace_id=r.workspace_id,
            name=r.name,
            repo_url=r.repo_url,
            default_branch=r.default_branch,
            created_at=r.created_at,
        )
        for r in repos
    ]
    return RepositoryListResponse(items=items, next_cursor=next_cursor)


@router.get(
    "/repositories/{repository_id}",
    response_model=RepositoryResponse,
)
async def get_repository(
    repository_id: uuid.UUID,
    caller: Caller = Depends(get_caller),
    db: AsyncSession = Depends(get_db),
) -> RepositoryResponse:
    repo = (
        await db.execute(select(Repository).where(Repository.id == repository_id))
    ).scalar_one_or_none()
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    # Authorize workspace access (ADR-0004: Byte-identical 404 for non-members)
    await authorize_workspace_access(
        db, caller, repo.workspace_id, min_role=WorkspaceRole.VIEWER, not_found_detail="Repository not found"
    )

    return RepositoryResponse(
        id=repo.id,
        workspace_id=repo.workspace_id,
        name=repo.name,
        repo_url=repo.repo_url,
        default_branch=repo.default_branch,
        created_at=repo.created_at,
    )
