"""定时任务管理页面（NL2Cron UI，方案 A：FastAPI 内置轻量管理页）。

GET /scheduler-ui → 返回 app/static/scheduler.html（原生 JS 单页，调用 /scheduler/* API）。
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["scheduler-ui"])

_STATIC_HTML = Path(__file__).resolve().parents[1] / "static" / "scheduler.html"


@router.get("/scheduler-ui", response_class=HTMLResponse)
def scheduler_ui() -> str:
    if not _STATIC_HTML.exists():
        return "<h1>scheduler.html 缺失：app/static/scheduler.html</h1>"
    return _STATIC_HTML.read_text(encoding="utf-8")
