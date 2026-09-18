"""FastAPI 应用入口。"""

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config.settings import settings
from app.observability.logging import setup_logging

setup_logging()

app = FastAPI(
    title="SweetNight Cross-border Multi-Agent System",
    version="0.1.0",
    debug=settings.APP_DEBUG,
)

app.include_router(health_router)


@app.get("/")
def root() -> dict:
    return {"service": "sweetnight-agent", "docs": "/docs"}
