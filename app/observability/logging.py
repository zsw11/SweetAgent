"""JSON 结构化日志（设计文档 43-45 节）。

统一输出字段：timestamp / trace_id / run_id / agent / node / tool / latency_ms / status 等。
禁止写入：完整敏感财务数据、密码、API Key、Token。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.config.settings import settings


def setup_logging(level: str | None = None) -> None:
    """初始化 structlog：开发环境可读格式，生产环境 JSON 格式。"""
    level = level or settings.LOG_LEVEL
    is_dev = settings.APP_ENV == "development"

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
    ]

    if is_dev:
        processors = shared_processors + [structlog.dev.ConsoleRenderer()]
    else:
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ]

    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s")
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    """获取 structlog logger。"""
    return structlog.get_logger(name)
