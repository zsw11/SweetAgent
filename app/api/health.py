"""健康检查接口。"""

from fastapi import APIRouter

from app.config.settings import settings

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
def health() -> dict:
    return {
        "status": "ok",
        "app_env": settings.APP_ENV,
        "version": "0.1.0",
    }
