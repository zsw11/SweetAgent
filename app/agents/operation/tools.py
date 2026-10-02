"""Operation Agent 工具注册（设计文档 11 节 Tools 清单）。

已实现（Phase 1 最小闭环）：
- schema 工具：list_tables / schema_search / get_table_schema / get_relationship / metric_definition
- SQL 生成与执行：generate_sql / repair_sql / ReadOnlyExecutor
- 分析：LLM 结构化分析（ANALYSIS_PROMPT）输出 evidence/metrics/anomalies

预留（Phase 2/3）：
- python_analysis_tool：复杂指标计算
- knowledge_search_tool：RAG 知识检索（依赖 app/knowledge 模块）
"""

from __future__ import annotations

from typing import Any, Callable

from app.tools.sql.executor import ReadOnlyExecutor
from app.tools.sql.generator import generate_sql, repair_sql
from app.tools.sql.schema import (
    get_relationship,
    get_table_schema,
    list_tables,
    metric_definition,
    schema_search,
)


def _tool(name: str, description: str, func: Callable[..., Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "func": func}


def get_operation_tools() -> list[dict[str, Any]]:
    """返回 Operation Agent 可用的工具注册表。"""
    return [
        # ---- Schema 元数据（设计文档 20 节：不给 LLM 整个数据库）----
        _tool(
            "list_tables",
            "列出 public schema 所有业务表（含注释）；prefix 可过滤如 'mart' 只看宽表。",
            list_tables,
        ),
        _tool(
            "schema_search",
            "按关键词搜索相关表/字段（返回候选表与命中字段），用于定位查询目标表。",
            schema_search,
        ),
        _tool(
            "get_table_schema",
            "获取指定表的字段/类型/注释，参数：table。",
            get_table_schema,
        ),
        _tool(
            "get_relationship",
            "获取指定表的外键关系，参数：table。",
            get_relationship,
        ),
        _tool(
            "metric_definition",
            "查询指标口径定义（agent_metric_definition），参数可选 metric_name。",
            metric_definition,
        ),
        # ---- SQL 生成 / 执行（唯一查数路径）----
        _tool(
            "generate_sql",
            "根据任务生成并通过安全校验的 SELECT SQL（LLM 或确定性模板）。",
            generate_sql,
        ),
        _tool(
            "repair_sql",
            "根据执行错误修正 SQL（LLM 模式自动修复）。",
            repair_sql,
        ),
        _tool(
            "execute_readonly_sql",
            "只读执行单条 SELECT（agent_reader 角色 + statement_timeout），返回 {columns, rows, row_count}。",
            ReadOnlyExecutor().execute,
        ),
        # 预留：模板 SQL 通道（考点五十八/五十九，未实现）——
        # 分层混合路由：模板兜高频固定口径（零成本）→ LLM 兜开放问答 → 成功 SQL 回流模板。
        # 将来如需把模板命中暴露给 LLM 可在此注册，例如：
        #   _tool("match_sql_template", "按任务匹配预置 SQL 模板，命中返回参数化 SQL。", match_template)
        # 注意：模板路由由 BaseDepartmentAgent._try_template 在 _query_one 前置调用，
        # 命中直出 observations、miss 走 generate_sql——不依赖本注册表即可工作。
    ]


def get_operation_tool_map() -> dict[str, Callable[..., Any]]:
    """返回 {工具名: 可调用对象}，供 Agent 内部按名调用。"""
    return {t["name"]: t["func"] for t in get_operation_tools()}
