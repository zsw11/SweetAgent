"""只读 SQL 执行器（设计文档 17-18 节）。

- 统一使用 agent_reader 只读数据库角色（仅 SELECT 权限）
- 强制 statement_timeout，防止慢查询拖垮数据库
- 记录执行耗时 / 行数，返回结构化结果

Agent 不允许直接操作 PostgreSQL，所有查询必须经过
SQL Generator -> SQL Validator -> ReadOnlyExecutor。

OPT-07（2026-09-27）结果缓存：
- 同 SQL + 同参数默认缓存（TTL = settings.SQL_CACHE_TTL_SECONDS，短 TTL 止血重复查询）；
- 缓存挂在模块级共享实例（execute_readonly_sql 工具每次 new 实例，实例级缓存=零命中）；
- 自动提取 FROM/JOIN 表名维护"表 → 缓存 key"索引，支持表级失效
  （invalidate_table，重灌/种子更新后调用，见 app.knowledge.ingest）；
- 命中时返回 cached=True 且 duration_ms≈0（可观测：日志 cache.hit + 返回值标记）。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from app.cache.ttl_cache import get_sql_cache
from app.config.settings import settings

# 表名提取：FROM / JOIN 后的标识符（可带 schema 前缀如 public.sales_daily）。
# 只用于"表级失效"的粗粒度关联，提取不到表名不影响正确性（TTL 兜底过期）。
_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:(?:[a-z_][a-z0-9_]*)\s*\.\s*)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)


def _cache_key(sql: str, params: Optional[tuple]) -> str:
    """缓存键：规范化 SQL（折叠空白，不改变语义，提高命中）+ 参数指纹。"""
    norm_sql = " ".join((sql or "").split())
    params_repr = json.dumps(list(params or []), ensure_ascii=False, default=str, sort_keys=True)
    digest = hashlib.sha256(f"{norm_sql}|{params_repr}".encode("utf-8")).hexdigest()
    return f"sql:{digest}"


def _extract_tables(sql: str) -> list[str]:
    """提取 SQL 涉及的 public 表名（小写去重排序）。"""
    return sorted({m.group(1).lower() for m in _TABLE_RE.finditer(sql or "")})


class ReadOnlyExecutor:
    """只读 SQL 执行器。

    Args:
        dsn: agent 只读角色连接串（默认取配置 AGENT_DATABASE_URL）。
        statement_timeout_ms: 单条查询超时（毫秒）。
    """

    # ---- 表级失效索引（类级共享：跨实例，任意线程可访问）----
    _table_index: dict[str, set[str]] = {}
    _index_lock = threading.RLock()

    @classmethod
    def invalidate_table(cls, table: str) -> int:
        """表级失效：删除所有涉及该表的缓存条目（重灌 / 种子更新后调用）。

        Returns: 实际删除的缓存条目数。
        """
        table = (table or "").lower()
        with cls._index_lock:
            keys = list(cls._table_index.get(table, ()))
        n = 0
        for k in keys:
            n += get_sql_cache().invalidate(k)
        with cls._index_lock:
            cls._table_index.pop(table, None)
        return n

    @classmethod
    def _register_table(cls, table: str, key: str) -> None:
        with cls._index_lock:
            cls._table_index.setdefault(table, set()).add(key)

    def __init__(
        self,
        dsn: Optional[str] = None,
        statement_timeout_ms: Optional[int] = None,
    ):
        self._dsn = dsn or settings.AGENT_DATABASE_URL
        self._timeout_ms = statement_timeout_ms or settings.SQL_STATEMENT_TIMEOUT_MS

    def execute(
        self,
        sql: str,
        params: Optional[tuple] = None,
        cache_ttl: Optional[int] = None,
    ) -> dict[str, Any]:
        """执行单条只读 SQL，返回 {columns, rows, row_count, duration_ms, cached}。

        OPT-07 缓存语义：
        - cache_ttl=None → 默认缓存（settings.SQL_CACHE_TTL_SECONDS，CACHE_ENABLED=False 时不缓存）；
        - cache_ttl=0 → 本次显式禁用缓存；
        - cache_ttl>0 → 覆盖默认 TTL（秒）。
        命中时 cached=True，duration_ms 为缓存读取耗时（≈0）。
        """
        if cache_ttl is None:
            cache_ttl = settings.SQL_CACHE_TTL_SECONDS if settings.CACHE_ENABLED else 0
        key = _cache_key(sql, params) if cache_ttl > 0 else None

        import time

        start = time.perf_counter()
        if key is not None:
            cached = get_sql_cache().get(key)
            if cached is not None:
                cached = dict(cached)
                # 命中时 duration_ms 必须是"缓存读取耗时"（≈0），而不是缓存里
                # 保存的首次实查耗时——否则命中与实查无法从耗时区分（可观测性破坏）。
                cached["duration_ms"] = int((time.perf_counter() - start) * 1000)
                cached["cached"] = True
                return cached

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
        result = {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "duration_ms": duration_ms,
            "cached": False,
        }
        if key is not None:
            get_sql_cache().set(key, result, cache_ttl)
            for table in _extract_tables(sql):
                self._register_table(table, key)
        return result
