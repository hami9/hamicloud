from fastapi import APIRouter

from app.api.v1.workspaces import router as workspaces_router
from app.api.v1.apps import router as apps_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.operations import router as operations_router
from app.api.v1.repositories import router as repositories_router
from app.api.v1.webhooks import router as webhooks_router
from app.api.v1.secrets import router as secrets_router
from app.api.v1.quotas import router as quotas_router
from app.api.v1.audit import router as audit_router

api_v1_router = APIRouter()

api_v1_router.include_router(workspaces_router)
api_v1_router.include_router(apps_router)
api_v1_router.include_router(jobs_router)
api_v1_router.include_router(operations_router)
api_v1_router.include_router(repositories_router)
api_v1_router.include_router(webhooks_router)
api_v1_router.include_router(secrets_router)
api_v1_router.include_router(quotas_router)
api_v1_router.include_router(audit_router)
