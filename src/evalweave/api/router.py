from fastapi import APIRouter

from evalweave.api.routes.admin import router as admin_router
from evalweave.api.routes.auth import router as auth_router
from evalweave.api.routes.health import router as health_router
from evalweave.api.routes.projects import router as projects_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(admin_router)
api_router.include_router(projects_router)
