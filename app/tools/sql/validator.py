"""SQL 语法级校验（设计文档 19 节）。

基于 sqlglot 做 AST 解析，Agent 生成的 SQL 必须通过本校验才能进入执行器。
检查项：
1. 必须是 SELECT
2. 禁止多语句
3. 禁止 DDL / DML
4. 禁止系统表（pg_catalog / information_schema）
5. 禁止敏感函数（pg_*、dblink 等）
6. 禁止 SELECT INTO
7. 禁止 COPY
8. 强制 LIMIT
9. 可选表白名单约束
"""

from __future__ import annotations

from typing import Iterable, Optional

import sqlglot
from sqlglot import expressions as exp

from app.config.settings import settings

# 系统 / 敏感 Schema 前缀
FORBIDDEN_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")
# 禁止的数据库级函数名前缀 / 名单
FORBIDDEN_FUNC_PREFIXES = ("pg_", "lo_import", "lo_export", "dblink", "copy")


class SQLValidationError(ValueError):
    """SQL 未通过安全校验。"""


def _forbidden_expression(node: exp.Expression) -> Optional[str]:
    """返回命中的禁止表达式类型，未命中返回 None。"""
    if isinstance(
        node,
        (
            exp.Insert,
            exp.Update,
            exp.Delete,
            exp.Drop,
            exp.Create,
            exp.Alter,
            exp.Copy,
            exp.Merge,
            exp.TruncateTable,
            exp.Command,
            exp.Transaction,
        ),
    ):
        return type(node).__name__

    # SELECT INTO
    if isinstance(node, exp.Select) and node.args.get("into"):
        return "SELECT INTO"

    # 敏感函数
    if isinstance(node, exp.Func):
        name = node.sql_name().lower()
        if name.startswith(FORBIDDEN_FUNC_PREFIXES) or name in {
            "pg_read_file",
            "pg_ls_dir",
            "lo_import",
            "lo_export",
        }:
            return f"forbidden function: {name}"

    return None


def _is_forbidden_table(table: exp.Table, allow_tables: Optional[set[str]] = None) -> Optional[str]:
    """检查表名是否命中系统表 / 白名单之外的表。"""
    db = (table.db or "").lower()
    name = (table.name or "").lower()

    if db in FORBIDDEN_SCHEMAS or name.startswith("pg_"):
        return f"system table: {table.sql()}"

    if allow_tables is not None:
        qualified = f"{db}.{name}" if db else name
        if qualified not in allow_tables and name not in allow_tables:
            return f"table not allowed: {table.sql()}"

    return None


def validate_sql(
    sql: str,
    *,
    require_limit: bool = True,
    max_limit: Optional[int] = None,
    allow_tables: Optional[set[str]] = None,
) -> str:
    """校验单条 SELECT SQL，通过则原样返回，失败抛 SQLValidationError。

    Args:
        sql: 待校验的 SQL（PostgreSQL 方言）。
        require_limit: 是否强制必须存在 LIMIT。
        max_limit: LIMIT 上限（默认取配置 SQL_MAX_LIMIT）。
        allow_tables: 表白名单（集合），为空表示不限制业务表。
    """
    max_limit = max_limit or settings.SQL_MAX_LIMIT
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except Exception as exc:  # sqlglot 解析失败
        raise SQLValidationError(f"SQL 解析失败: {exc}") from exc

    if not statements or len(statements) != 1:
        raise SQLValidationError("只允许单条 SQL 语句")

    stmt = statements[0]
    if not isinstance(stmt, exp.Select):
        raise SQLValidationError(f"只允许 SELECT 语句，当前为 {type(stmt).__name__}")

    # 遍历整棵 AST，禁止任何 DML / 系统函数 / SELECT INTO
    for node in stmt.walk():
        hit = _forbidden_expression(node)
        if hit:
            raise SQLValidationError(f"检测到禁止操作: {hit}")

        if isinstance(node, exp.Table):
            hit = _is_forbidden_table(node, allow_tables)
            if hit:
                raise SQLValidationError(hit)

    # 强制 LIMIT
    limit_expr = stmt.args.get("limit")
    if require_limit and limit_expr is None:
        raise SQLValidationError("必须包含 LIMIT 子句（防止全表扫描）")

    if limit_expr is not None:
        limit_value = limit_expr.expression
        try:
            if isinstance(limit_value, exp.Literal) and int(limit_value.this) > max_limit:
                raise SQLValidationError(f"LIMIT 超过上限 {max_limit}")
        except (ValueError, TypeError):
            raise SQLValidationError("LIMIT 必须是整数") from None

    return stmt.sql(dialect="postgres")


def add_limit(sql: str, limit: int = 1000) -> str:
    """为无 LIMIT 的 SELECT 注入 LIMIT（用于执行兜底）。"""
    statements = sqlglot.parse(sql, read="postgres")
    if not statements or len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise SQLValidationError("只允许单条 SELECT 语句")
    stmt = statements[0]
    if stmt.args.get("limit") is None:
        stmt = stmt.limit(limit)
    return stmt.sql(dialect="postgres")


def validate_and_bind_limit(sql: str, **kwargs) -> str:
    """校验通过后返回最终可执行 SQL：已有 LIMIT 则校验上限，无 LIMIT 则注入默认值。"""
    validate_sql(sql, require_limit=False, **kwargs)
    return add_limit(sql, limit=kwargs.get("max_limit") or settings.SQL_MAX_LIMIT)
