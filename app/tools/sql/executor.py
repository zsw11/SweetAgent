"""只读 SQL 执行器（设计文档 17-18 节）。

- 统一使用 agent_reader 只读数据库角色（仅 SELECT 权限）
- 强制 statement_timeout，防止慢查询拖垮数据库
- 记录执行耗时 / 行数，返回结构化结果

Agent 不允许直接操作 PostgreSQL，所有查询必须经过
SQL Generator -> SQL Validator -> ReadOnlyExecutor。
"""

from __future__ import annotations

from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from app.config.settings import settings


class ReadOnlyExecutor:
    """只读 SQL 执行器。

    Args:
        dsn: agent 只读角色连接串（默认取配置 AGENT_DATABASE_URL）。
        statement_timeout_ms: 单条查询超时（毫秒）。
    """

    def __init__(
        self,
        dsn: Optional[str] = None,
        statement_timeout_ms: Optional[int] = None,
    ):
        self._dsn = dsn or settings.AGENT_DATABASE_URL
        self._timeout_ms = statement_timeout_ms or settings.SQL_STATEMENT_TIMEOUT_MS

    def execute(self, sql: str, params: Optional[tuple] = None) -> dict[str, Any]:
        """执行单条只读 SQL，返回 {columns, rows, row_count, duration_ms}。

        TODO(Phase 2): 增加结果截断 / 敏感列脱敏 / 慢查询指标上报。
        """
        import time

        start = time.perf_counter()
        try:
            with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SET statement_timeout = {self._timeout_ms}")
                    cur.execute(sql, params)
                    columns = [d.name for d in cur.description] if cur.description else []
                    rows = cur.fetchall()
        except psycopg.Error as exc:
            raise RuntimeError(f"SQL 执行失败: {exc}") from exc

        duration_ms = int((time.perf_counter() - start) * 1000)
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "duration_ms": duration_ms,
        }
