from fastapi import APIRouter

from evalweave.core.config import get_settings

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "name": settings.application.name,
        "version": settings.application.version,
    }
