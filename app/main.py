"""FastAPI 应用入口。"""

import sys
from pathlib import Path

# 脚本以 app/main.py 直接运行时，sys.path[0] 是 app/ 而非项目根，
# 顶部任何 from app.xxx import 都会 ModuleNotFoundError——必须在首个 app.* import 前修正。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from contextlib import asynccontextmanager

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时恢复并注册定时任务（NL2Cron，2026-10-02，考点六十四）。

    scheduler_jobs 表是唯一权威源：重启后从 DB 重放已审核通过的任务进 APScheduler。
    启动失败仅降级警告（调度不可用不影响主查询链路）；关闭时优雅关停。
    """
    if settings.SCHEDULER_ENABLED:
        try:
            from app.scheduler.service import restore_jobs
            restored = restore_jobs()
            if restored > 0:
                from app.observability.logging import get_logger
                get_logger("main").info("scheduler.restored_at_startup", count=restored)
        except Exception as exc:
            from app.observability.logging import get_logger
            get_logger("main").warning("scheduler.startup.fail", error=str(exc)[:300])
    yield
    if settings.SCHEDULER_ENABLED:
        try:
            from app.scheduler.service import get_scheduler
            sched = get_scheduler()
            if sched.running:
                sched.shutdown(wait=False)
        except Exception:
            pass


app = FastAPI(
    title="SweetNight Cross-border Multi-Agent System",
    version="0.1.0",
    debug=settings.APP_DEBUG,
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(chat_router)
app.include_router(memory_router)

from app.scheduler.api import router as scheduler_router

app.include_router(scheduler_router)

from app.api.scheduler_ui import router as scheduler_ui_router

app.include_router(scheduler_ui_router)


@app.get("/")
def root() -> dict:
    return {"service": "sweetnight-agent", "docs": "/docs"}


if __name__ == "__main__":
    """直接运行入口（PyCharm Run app/main.py 亦可启动，等价 uvicorn app.main:app）。

    --reload 仅开发期使用；生产由外部进程管理器（systemd/supervisor）拉起 uvicorn，
    不经过本入口。
    """
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=settings.APP_DEBUG,
    )
