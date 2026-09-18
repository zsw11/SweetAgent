"""短期状态快照（设计文档 33 节）。

使用 PostgresSaver 持久化 LangGraph State，支持：
中断 -> 恢复、故障重试、time travel。

TODO(Phase 1): 初始化 PostgresSaver（langgraph-checkpoint-postgres）。
"""

from __future__ import annotations

from typing import Optional

from app.config.settings import settings


def get_checkpointer():
    """返回 LangGraph 兼容的 checkpointer。

    TODO(Phase 1): 实现基于 psycopg 的 PostgresSaver 实例。
    """
    raise NotImplementedError("Phase 1 实现（PostgresSaver）")


def get_postgres_store():
    """返回长期记忆 PostgresStore（结构化 key-value）。

    TODO(Phase 6): 实现 PostgresStore / 自建 user_profiles 等表。
    """
    raise NotImplementedError("Phase 6 实现")
