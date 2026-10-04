"""SQL 生成器（设计文档 5 节 / 58 节最小闭环，LLM 模式）。

流程：LLM 根据 Schema 上下文生成 SQL -> Validator 校验 -> 失败重试。

仅支持 LLM 模式（需配置 OpenAI 兼容 Key）：生成与修复均调用 LLM，
Schema 结构 + 业务数据字典注入，防编造表名/字段。
"""

from __future__ import annotations

from typing import Any, Optional

from app.config.settings import settings
from app.llm import get_chat_model
from app.llm.structured import invoke_text, invoke_tool
from app.observability.logging import get_logger
from app.tools.sql.validator import SQLValidationError, validate_and_bind_limit

logger = get_logger("sql.generator")

# ---------------------------------------------------------------------------
# OPT-05 Function Calling 原生化：SQL 生成/修复走原生 tool_calls 通道
# （LLM 的 SQL 输出进入结构化 arguments，不再依赖文本 + markdown 剥离）
# ---------------------------------------------------------------------------

_SQL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_sql",
            "description": (
                "根据任务、可用表结构、业务数据字典，生成一条 PostgreSQL SELECT 查询。"
                "必须只读 SELECT、必须带 LIMIT（不超过 5000）、只能使用给定表结构中的字段。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "生成的 SELECT SQL 语句"},
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repair_sql",
            "description": "根据执行错误修正一条失败的 SELECT SQL。保持只读 SELECT、带 LIMIT。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fixed_sql": {"type": "string", "description": "修正后的 SELECT SQL 语句"},
                },
                "required": ["fixed_sql"],
            },
        },
    },
]


def _invoke_sql(model, system: str, user: str, tool_index: int) -> str:
    """走原生 tool_calls 通道取 SQL；失败降级文本回复。

    Args:
        model: ChatModel。
        system/user: 提示词。
        tool_index: _SQL_TOOLS 下标（0=generate_sql，1=repair_sql）。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    key = "sql" if tool_index == 0 else "fixed_sql"
    sql, text = invoke_tool(model, _SQL_TOOLS[tool_index], messages, key, logger_name="sql.generator")
    if sql is not None:
        return sql
    if text is not None:
        return text
    return invoke_text(model, messages, logger_name="sql.generator") or ""


def _llm_generate(task: str, schema_context: dict[str, Any], model) -> str:
    """LLM 生成 SQL（OPT-05：原生 tool_calls 通道，失败降级文本）。"""
    schema_text = "\n\n".join(
        f"表 {t.get('table')}: {t.get('columns')}" for t in schema_context.get("tables", [])
    )
    metrics = schema_context.get("metrics", [])
    metrics_text = "\n".join(
        f"- {m.get('metric_name')}: {m.get('definition')} ({m.get('formula')}, 单位 {m.get('unit')})"
        for m in metrics
    )
    dictionary = schema_context.get("dictionary", "")

    system = (
        "你是跨境电商数据库 SQL 专家。根据任务、表结构和业务数据字典，生成一条 PostgreSQL SELECT 查询。\n"
        "硬性要求：\n"
        "1. 只允许单条 SELECT，禁止 DML/DDL/多语句/系统表；\n"
        "2. 必须带 LIMIT（不超过 5000）；\n"
        "3. 只能使用给定表结构中的字段；\n"
        "4. 品牌/市场等过滤必须依据数据字典中的真实取值与关联方式，禁止臆造；\n"
        "5. 通过工具调用输出 SQL（arguments.sql），不要解释。"
    )
    user = (
        f"任务：{task}\n\n"
        f"可用表结构：\n{schema_text}\n\n"
        f"业务数据字典（必须遵守）：\n{dictionary or '(无)'}\n\n"
        f"指标口径（agent_metric_definition）：\n{metrics_text or '(无)'}\n\n"
        "请调用 generate_sql 工具并输出 SQL："
    )
    sql = _invoke_sql(model, system, user, tool_index=0)
    logger.debug(
        "sql.generate.llm",
        model=model.model_name if hasattr(model, "model_name") else str(type(model).__name__),
        sql=sql,
    )
    return sql


def generate_sql(task: str, context: Optional[dict[str, Any]] = None, model=None) -> str:
    """根据任务与 Schema 上下文生成并通过校验的 SQL（LLM 模式）。

    Args:
        task: 子任务描述。
        context: {"tables": [schema], "metrics": [...], "dictionary": str} 等上下文。
        model: 可选 ChatModel；不传时自动创建。

    Returns:
        校验通过的可执行 SQL（已注入/校验 LIMIT）。

    Raises:
        SQLValidationError: LLM 生成的 SQL 多次校验失败。
    """
    context = context or {}
    m = model or get_chat_model(tier="medium")
    last_err: Optional[str] = None
    for attempt in range(settings.MAX_SQL_RETRIES):
        try:
            sql = _llm_generate(task, context, m)
            return validate_and_bind_limit(sql, max_limit=settings.SQL_MAX_LIMIT)
        except SQLValidationError as exc:
            last_err = str(exc)
        except Exception as exc:  # LLM 调用 / 网络失败
            last_err = str(exc)

    raise SQLValidationError(f"SQL 生成失败（{settings.MAX_SQL_RETRIES} 次尝试）: {last_err}")


def repair_sql(sql: str, error: str, context: Optional[dict[str, Any]] = None, model=None) -> str:
    """根据执行错误修正 SQL（设计文档 40 节 B 类，最多 MAX_SQL_RETRIES 次，LLM 模式）。"""
    context = context or {}
    m = model or get_chat_model(tier="medium")
    system = (
        "你是 SQL 专家。上一条 SQL 执行失败，请根据错误信息修正后通过调用 repair_sql 工具输出"
        "（arguments.fixed_sql）。保持 SELECT-only、带 LIMIT。"
    )
    user = f"原 SQL：\n{sql}\n\n错误：\n{error}\n\n请调用 repair_sql 工具输出修正后的 SQL："
    fixed_raw = _invoke_sql(m, system, user, tool_index=1)
    # 兜底：剥离 LLM 可能包裹的 markdown 代码块（```sql ... ```），避免 validator 解析失败
    if fixed_raw.startswith("```"):
        fixed_raw = fixed_raw.strip("`").strip()
        if fixed_raw.lower().startswith("sql"):
            fixed_raw = fixed_raw[3:].strip()
    fixed = validate_and_bind_limit(fixed_raw, max_limit=settings.SQL_MAX_LIMIT)
    logger.debug("sql.repair", reason=error[:300], sql_old=sql[:1000], sql_new=fixed[:1000])
    return fixed
