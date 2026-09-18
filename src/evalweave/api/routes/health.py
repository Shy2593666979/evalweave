from fastapi import APIRouter

from evalweave.api.response import APIResponse
from evalweave.core.config import get_settings

router = APIRouter(tags=["system"])


@router.get("/health", response_model=APIResponse[dict[str, str]])
def health() -> APIResponse[dict[str, str]]:
    settings = get_settings()
    return APIResponse.success({
        "status": "ok",
        "name": settings.application.name,
        "version": settings.application.version,
    })
