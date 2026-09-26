"""LangSmith 可观测性接入（OPT-02，2026-09-26）。

接入方式：**LangChain / LangGraph 环境变量自动 tracing + langsmith SDK 客户端**。
- 设置 `LANGSMITH_TRACING=true` + API key + project 后，LangChain/LangGraph 在运行时
  自动上报全链路 trace：LLM 调用（输入/输出/token/耗时）、工具调用、graph 节点、
  quality_gate 判定、并行分支 —— 业务代码零侵入。
- 关键坑：项目用 pydantic-settings 从 .env 读配置，**读到的值不会写回 os.environ**；
  而 LangChain tracer 和 langsmith SDK 只认环境变量。本模块负责把 settings 同步到
  os.environ，再初始化 Client，作为唯一入口。
- `init_langsmith()` **幂等**：可同时挂在 FastAPI 启动与 run_question 入口，重复调用零副作用。

数据上云说明：开启后一次真实提问的完整链路（含 LLM 输入输出原文）会上报
smith.langchain.com；敏感数据请确认合规，或把 .env 的 LANGSMITH_TRACING 置 false
退回仅本地日志（app/observability/logging.py + metrics.py 不受影响）。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("langsmith")

_initialized = False
_client: Optional[Any] = None


def init_langsmith() -> Optional[Any]:
    """同步环境变量并初始化 LangSmith Client（幂等）。

    Returns:
        langsmith Client（启用且验证通过）或 None（未启用 / key 缺失 / 网络失败）。
        返回 None 只影响可观测性，不影响主链路。
    """
    global _initialized, _client
    if _initialized:
        return _client
    _initialized = True

    tracing = "true" if settings.LANGSMITH_TRACING else "false"
    api_key = settings.LANGSMITH_API_KEY
    project = settings.LANGSMITH_PROJECT

    # pydantic-settings 不写回 os.environ，这里显式同步（LangChain tracer / SDK 只认环境变量）
    os.environ["LANGSMITH_TRACING"] = tracing
    os.environ["LANGCHAIN_TRACING_V2"] = tracing  # 旧变量名，兼容 langchain <1.0 生态
    os.environ["LANGSMITH_API_KEY"] = api_key
    os.environ["LANGSMITH_PROJECT"] = project

    if not settings.LANGSMITH_TRACING or not api_key:
        logger.info(
            "langsmith.disabled", tracing=tracing, has_key=bool(api_key), project=project
        )
        return None

    try:
        from langsmith import Client

        client = Client()
        # 轻量验证：key 是否有效 / 网络是否可达（失败仅降级警告，不炸主链路）
        client.list_projects(limit=1)
        _client = client
        logger.info("langsmith.ready", project=project, api_url=getattr(client, "api_url", ""))
        return client
    except Exception as exc:  # noqa: BLE001 - 可观测性失败不允许影响业务
        logger.warning("langsmith.init_fail", error=str(exc)[:300], project=project)
        return None


def get_langsmith_client() -> Optional[Any]:
    """返回已初始化的 Client；未初始化时先初始化。"""
    if _client is None:
        init_langsmith()
    return _client


def list_recent_runs(limit: int = 5, run_type: Optional[str] = None) -> list[dict]:
    """列出最近 trace（验证/教学用），返回 [{run_id, name, start_time, url}]。"""
    client = get_langsmith_client()
    if client is None:
        return []
    try:
        runs = client.list_runs(
            project_name=settings.LANGSMITH_PROJECT,
            run_type=run_type,
            limit=limit,
        )
        out: list[dict] = []
        for run in runs:
            out.append({
                "run_id": str(getattr(run, "id", "")),
                "name": getattr(run, "name", ""),
                "start_time": str(getattr(run, "start_time", "")),
                "url": _run_url(client, run),
            })
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("langsmith.list_runs_fail", error=str(exc)[:300])
        return []


def get_run_url_by_id(run_id: str) -> str:
    """按 run_id 构造 trace 的 web 链接（未启用 / run 不存在返回空串）。

    供 run_question 在返回结果里带 trace 链接（排障时从应用直接点进面板）。
    """
    client = get_langsmith_client()
    if client is None or not run_id:
        return ""
    try:
        run = client.read_run(run_id)
        return client.get_run_url(run=run, project_name=settings.LANGSMITH_PROJECT)
    except Exception:
        return ""


def _run_url(client: Any, run: Any) -> str:
    """构造 run 的 web 链接（失败时返回空串）。

    langsmith >=0.12 的 get_run_url 签名：get_run_url(*, run, project_name=...)，
    传 Run 对象而非 run_id。
    """
    try:
        return client.get_run_url(run=run, project_name=settings.LANGSMITH_PROJECT)
    except Exception:
        return ""
