"""FastAPI 应用入口。"""

from fastapi import FastAPI

from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.api.memory import router as memory_router
from app.config.settings import settings
from app.observability.logging import setup_logging
from app.observability.tracing import init_langsmith

setup_logging()
# OPT-02 LangSmith：启动即同步环境变量并初始化 Client（幂等，失败仅降级警告）
init_langsmith()

app = FastAPI(
    title="SweetNight Cross-border Multi-Agent System",
    version="0.1.0",
    debug=settings.APP_DEBUG,
)

app.include_router(health_router)
app.include_router(chat_router)
app.include_router(memory_router)


@app.get("/")
def root() -> dict:
    return {"service": "sweetnight-agent", "docs": "/docs"}
