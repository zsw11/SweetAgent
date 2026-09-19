"""短期状态快照（设计文档 33 节）。

使用 PostgresSaver 持久化 LangGraph State，支持：
中断 -> 恢复、故障重试、time travel（为第 11 项 Interrupt 打基础）。

实现要点：
- 连接串用 settings.DATABASE_URL（app_user 写角色，checkpoints 表归它管）
- psycopg_pool.ConnectionPool 线程安全，FastAPI 多线程下可用
- 池连接 autocommit=True：PostgresSaver.setup() 内含 CREATE INDEX CONCURRENTLY，
  不能在事务块内运行（psycopg 默认隐式事务会报 ActiveSqlTransaction）；
  langgraph-checkpoint-postgres 官方 from_conn_string 同样使用 autocommit 连接
- 进程内单例缓存，避免每次调用重建连接池 / 重复建表
- atexit 关闭连接池，避免解释器退出时 ConnectionPool.__del__ 线程清理噪音
"""

from __future__ import annotations

import atexit
from typing import Optional

from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("checkpoint")

_pool = None
_checkpointer = None


def _get_pool():
    """获取 psycopg 连接池（单例，延迟打开，autocommit）。"""
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(
            conninfo=settings.DATABASE_URL,
            max_size=10,
            open=False,  # 延迟打开，避免模块导入即连库
            kwargs={"autocommit": True},  # setup() 的 CREATE INDEX CONCURRENTLY 需要非事务
        )
        _pool.open()
        logger.info("checkpoint.pool_ready", max_size=10)
    return _pool


def _close_pool() -> None:
    """解释器退出前关闭连接池（atexit 注册，早于 __del__ 触发）。"""
    global _pool
    if _pool is not None:
        try:
            _pool.close()
            logger.info("checkpoint.pool_closed")
        except Exception:
            pass


atexit.register(_close_pool)


def get_checkpointer():
    """返回 LangGraph 兼容的 checkpointer（单例，PostgresSaver + 连接池）。

    setup() 幂等：自动创建 checkpoints / checkpoint_writes 表（app_user 有写权限）。
    """
    global _checkpointer
    if _checkpointer is None:
        from langgraph.checkpoint.postgres import PostgresSaver

        pool = _get_pool()
        _checkpointer = PostgresSaver(pool)
        _checkpointer.setup()
        logger.info("checkpoint.ready", table=settings.CHECKPOINTER_TABLE)
    return _checkpointer


def get_postgres_store():
    """返回长期记忆 PostgresStore（结构化 key-value）。

    TODO(Phase 6): 实现 PostgresStore / 自建 user_profiles 等表。
    """
    raise NotImplementedError("Phase 6 实现")
