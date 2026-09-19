from fastapi import APIRouter

from evalweave.api.routes.admin import router as admin_router
from evalweave.api.routes.agents import router as agents_router
from evalweave.api.routes.auth import router as auth_router
from evalweave.api.routes.files import router as files_router
from evalweave.api.routes.health import router as health_router
from evalweave.api.routes.human_reviews import router as human_reviews_router
from evalweave.api.routes.projects import router as projects_router
from evalweave.api.routes.scheduled_evaluations import router as scheduled_evaluations_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(admin_router)
api_router.include_router(agents_router)
api_router.include_router(scheduled_evaluations_router)
api_router.include_router(human_reviews_router)
api_router.include_router(projects_router)
api_router.include_router(files_router)
