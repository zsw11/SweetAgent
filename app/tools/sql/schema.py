"""Schema 元数据工具（设计文档 20 节）。

不要给 LLM 整个数据库，只暴露结构化元数据：
- list_tables：有哪些表（mart_* 宽表优先展示）
- get_table_schema：指定表的字段 / 类型 / 注释
- get_relationship：表间外键关系
- schema_search：按关键词搜索相关表 / 字段
- metric_definition：指标口径定义（agent_metric_definition 表）

统一使用 agent_reader 只读角色连接，只读元数据，不暴露任何业务数据。
"""

from __future__ import annotations

from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from app.config.settings import settings


def _connect():
    """打开只读元数据连接。"""
    return psycopg.connect(settings.AGENT_DATABASE_URL, row_factory=dict_row)


def list_tables(schema: str = "public", prefix: Optional[str] = None) -> list[dict[str, Any]]:
    """列出可查询的表（仅表名 / 注释，不暴露数据）。

    Args:
        schema: schema 名，默认 public。
        prefix: 可选前缀过滤（如 "mart" 只看宽表）。
    """
    sql = """
        SELECT t.table_name,
               obj_description(c.oid) AS table_comment
        FROM information_schema.tables t
        JOIN pg_catalog.pg_class c
          ON c.relname = t.table_name
         AND c.relnamespace = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = %s)
        WHERE t.table_schema = %s
          AND t.table_type = 'BASE TABLE'
          AND (%s::text IS NULL OR t.table_name LIKE %s)
        ORDER BY t.table_name
    """
    pattern = f"{prefix}%" if prefix else None
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (schema, schema, pattern, pattern))
            return cur.fetchall()


def get_table_schema(table: str, schema: str = "public") -> dict[str, Any]:
    """获取单表 Schema（字段名 / 类型 / 可空 / 默认值 / 注释）。

    返回 {table, columns: [...]}；表不存在返回 {table, columns: [], error}。
    """
    sql = """
        SELECT c.column_name,
               c.data_type,
               CASE WHEN c.udt_name = 'vector' THEN 'vector(' || c.character_maximum_length || ')'
                    ELSE c.udt_name END AS udt_name,
               c.is_nullable,
               c.column_default,
               col_description(
                   (SELECT oid FROM pg_catalog.pg_class
                    WHERE relname = %s
                      AND relnamespace = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = %s)),
                   c.ordinal_position
               ) AS column_comment
        FROM information_schema.columns c
        WHERE c.table_schema = %s AND c.table_name = %s
        ORDER BY c.ordinal_position
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (table, schema, schema, table))
            rows = cur.fetchall()
    if not rows:
        return {"table": table, "columns": [], "error": f"表 {table} 不存在或不在 {schema} schema"}
    return {"table": table, "columns": rows}


def get_relationship(table: str, schema: str = "public") -> list[dict[str, Any]]:
    """获取表的外键关系（子表 -> 父表）。"""
    sql = """
        SELECT tc.table_name        AS child_table,
               kcu.column_name      AS child_column,
               ccu.table_name       AS parent_table,
               ccu.column_name      AS parent_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = %s
          AND tc.table_name = %s
        ORDER BY kcu.ordinal_position
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (schema, table))
            return cur.fetchall()


def schema_search(keyword: str, limit: int = 20) -> list[dict[str, Any]]:
    """按关键词搜索相关表 / 字段（基于表名 / 字段名 / 注释，不暴露数据）。

    返回候选表列表，供 LLM 判断相关性。
    """
    sql = """
        SELECT t.table_name,
               COUNT(c.column_name) AS column_hits,
               STRING_AGG(c.column_name, ', ' ORDER BY c.column_name) AS matched_columns
        FROM information_schema.tables t
        JOIN information_schema.columns c
          ON c.table_schema = t.table_schema AND c.table_name = t.table_name
        WHERE t.table_schema = 'public'
          AND t.table_type = 'BASE TABLE'
          AND (
               t.table_name ILIKE %s
               OR c.column_name ILIKE %s
               OR obj_description(
                    (SELECT oid FROM pg_catalog.pg_class
                     WHERE relname = t.table_name
                       AND relnamespace = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = 'public'))
               ) ILIKE %s
          )
        GROUP BY t.table_name
        ORDER BY column_hits DESC, t.table_name
        LIMIT %s
    """
    pattern = f"%{keyword}%"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (pattern, pattern, pattern, limit))
            return cur.fetchall()


def metric_definition(metric_name: Optional[str] = None) -> list[dict[str, Any]]:
    """查询指标口径定义（agent_metric_definition 表）。

    Args:
        metric_name: 可选指标名过滤；为空返回全部。
    """
    sql = """
        SELECT metric_name, definition, formula, unit, department
        FROM agent_metric_definition
        WHERE (%s::text IS NULL OR metric_name ILIKE %s)
        ORDER BY department, metric_name
    """
    pattern = f"%{metric_name}%" if metric_name else None
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (pattern, pattern))
            return cur.fetchall()
