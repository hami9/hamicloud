from fastapi import APIRouter

from app.api.v1.workspaces import router as workspaces_router
from app.api.v1.apps import router as apps_router
from app.api.v1.jobs import router as jobs_router

api_v1_router = APIRouter()

api_v1_router.include_router(workspaces_router)
api_v1_router.include_router(apps_router)
api_v1_router.include_router(jobs_router)
