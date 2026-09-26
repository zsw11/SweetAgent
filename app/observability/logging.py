"""JSON 结构化日志（设计文档 43-45 节）。

统一输出字段：timestamp / trace_id / run_id / agent / node / tool / latency_ms / status 等。
禁止写入：完整敏感财务数据、密码、API Key、Token。

输出双通道（2026-09-26 起）：
- stdout：开发环境可读格式（ConsoleRenderer），生产环境 JSON；
- 文件 logs/app.log：始终 JSON（RotatingFileHandler 5MB×3 轮转），便于检索与留档。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Any

import structlog

from app.config.settings import BASE_DIR, settings


def setup_logging(level: str | None = None) -> None:
    """初始化 structlog：stdout + 文件双输出。

    stdout：开发环境可读格式（ConsoleRenderer），生产环境 JSON；
    文件：logs/app.log，JSON 格式，5MB×3 轮转。
    """
    level = level or settings.LOG_LEVEL
    is_dev = settings.APP_ENV == "development"

    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)

    # 根 logging：stdout + 文件两个 handler（structlog 事件经 stdlib 管道进入二者）
    stream_handler = logging.StreamHandler(sys.stdout)
    file_handler = RotatingFileHandler(
        logs_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        handlers=[stream_handler, file_handler],
        level=level.upper(),
        format="%(message)s",
        force=True,
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    stream_formatter = structlog.stdlib.ProcessorFormatter(
        processor=(
            structlog.dev.ConsoleRenderer()
            if is_dev
            else structlog.processors.JSONRenderer(ensure_ascii=False)
        ),
        foreign_pre_chain=shared_processors,
    )
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(ensure_ascii=False),
        foreign_pre_chain=shared_processors,
    )
    stream_handler.setFormatter(stream_formatter)
    file_handler.setFormatter(file_formatter)


def get_logger(name: str):
    """获取 structlog logger。"""
    return structlog.get_logger(name)
