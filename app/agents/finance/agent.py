"""Finance Agent 主逻辑（设计文档 12 节）。

继承 OperationAgent，复用内部循环（plan→query→analyze→retry），
仅覆盖数据域白名单、优先级表、数据字典和 Prompt。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.agents.finance.prompts import ANALYSIS_PROMPT, PLAN_PROMPT
from app.agents.finance.state import FinanceState
from app.agents.finance.tools import get_finance_tool_map
from app.agents.operation.agent import (
    OperationAgent,
    _collect_anomalies,
    _collect_metrics,
    _parse_analysis_json,
)
from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("finance_agent")

# 财务数据域白名单
_KNOWN_REQS: frozenset[str] = frozenset({"profit", "cost", "revenue", "refund", "platform_fee"})


class FinanceAgent(OperationAgent):
    """财务 Agent：利润/成本/费用/退款分析（LLM 模式）。"""

    # 覆盖白名单
    _KNOWN_REQS_CLASS: frozenset[str] = _KNOWN_REQS

    # 各数据域的核心表（优先注入 schema 上下文）
    _REQ_PRIORITY_TABLES: dict[str, list[str]] = {
        "profit": ["mart_product_profit_daily", "profit_daily", "product_skus", "brands"],
        "cost": ["cost_daily", "financial_transactions", "stores"],
        "revenue": ["revenue_daily", "financial_transactions", "stores"],
        "refund": ["refunds", "orders", "product_skus"],
        "platform_fee": ["platform_fees", "orders", "stores"],
    }

    def __init__(self, model=None, executor=None):
        # 直接初始化父类，但用财务工具
        from app.llm import get_chat_model, llm_available
        if not llm_available():
            raise RuntimeError("未配置 LLM API Key。Finance Agent 需要 LLM。")
        self.llm_ready = True
        self.model = model if model is not None else get_chat_model(tier="medium")
        self.tools = get_finance_tool_map()
        self.executor = executor or self.tools["execute_readonly_sql"]
        self.max_iterations = settings.MAX_AGENT_ITERATIONS
        self.max_sql_retries = settings.MAX_SQL_RETRIES

    def _plan(self, task: str, context: dict[str, Any]) -> list[str]:
        """LLM 规划财务数据需求。"""
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            resp = self.model.invoke([
                SystemMessage(content="你是财务分析 Agent 的规划器，只输出数据域名称列表。"),
                HumanMessage(content=PLAN_PROMPT.format(task=task)),
            ])
            plan = [ln.strip().lower() for ln in str(resp.content).splitlines() if ln.strip()]
            plan = [p for p in plan if p in _KNOWN_REQS]
            if "profit" not in plan:
                plan.insert(0, "profit")
            return plan
        except Exception as exc:
            logger.warning("finance.plan.fallback", error=str(exc))
            return ["profit"]

    def _build_schema_context(self, req: str, base: dict[str, Any]) -> dict[str, Any]:
        """构建 schema 上下文 + 财务数据字典。"""
        keyword = {
            "profit": "profit",
            "cost": "cost",
            "revenue": "revenue",
            "refund": "refund",
            "platform_fee": "fee",
        }.get(req, req)
        try:
            table_names: list[str] = []
            for t in self._REQ_PRIORITY_TABLES.get(req, []):
                if t not in table_names:
                    table_names.append(t)
            hits = self.tools["schema_search"](keyword, limit=8)
            for h in hits:
                if h["table_name"] not in table_names:
                    table_names.append(h["table_name"])
            tables = []
            for name in table_names[:6]:
                try:
                    t = self.tools["get_table_schema"](name)
                    if not t.get("error"):
                        tables.append(t)
                except Exception:
                    continue
            metrics = self.tools["metric_definition"]()
            dictionary = self._load_dictionary()
        except Exception as exc:
            logger.warning("finance.schema_context.fail", error=str(exc))
            tables, metrics, dictionary = [], [], ""
        return {**base, "tables": tables, "metrics": metrics, "dictionary": dictionary}

    def _load_dictionary(self) -> str:
        """加载财务数据字典。"""
        lines: list[str] = []
        try:
            r = self.executor("SELECT id, name FROM brands ORDER BY id LIMIT 20")
            lines.append("品牌: " + ", ".join(f"{row['id']}={row['name']}" for row in r["rows"]))
        except Exception:
            pass
        try:
            r = self.executor("SELECT DISTINCT country FROM profit_daily ORDER BY 1 LIMIT 20")
            lines.append("市场取值: " + ", ".join(str(row["country"]) for row in r["rows"]))
        except Exception:
            pass
        try:
            r = self.executor("SELECT MIN(date) AS d0, MAX(date) AS d1 FROM profit_daily")
            row = r["rows"][0] if r["rows"] else {}
            lines.append(f"数据时间窗口: {row.get('d0')} ~ {row.get('d1')}（共约 90 天；last21 = 最后21天）")
        except Exception:
            pass
        lines.append(
            "查询提示: ①品牌过滤用 JOIN brands 按 name 匹配；"
            "②时间分段用 date > (SELECT MAX(date) - 21 FROM 主表)；"
            "③两段天数不同，对比必须换算为日均（SUM(x)/COUNT(DISTINCT date)）；"
            "④利润表 mart_product_profit_daily 已含 revenue/product_cost/platform_fee/advertising_cost/"
            "logistics_cost/refund_cost/gross_profit/contribution_profit/profit_margin，优先用此宽表。"
        )
        text = "\n".join(lines)
        logger.debug("finance.dictionary", text=text[:1500])
        return text

    def _analyze(self, task: str, observations: list[dict[str, Any]]):
        """LLM 财务分析。"""
        raw = ""
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            resp = self.model.invoke([
                SystemMessage(content=ANALYSIS_PROMPT.split("任务：")[0]),
                HumanMessage(content=ANALYSIS_PROMPT.format(
                    task=task,
                    result_json=json.dumps(observations, ensure_ascii=False, default=str),
                )),
            ])
            raw = str(resp.content)
            logger.debug("finance.analyze.llm", raw=raw[:2000])
            parsed = _parse_analysis_json(raw)
            if parsed:
                analysis = [parsed.get("summary", "")]
                evidence = [{"type": "analysis", **parsed}]
                for m in parsed.get("metrics") or []:
                    if isinstance(m, dict):
                        evidence.append({"type": "llm_metric", **m})
                for a in parsed.get("anomalies") or []:
                    if isinstance(a, dict):
                        evidence.append({"type": "llm_anomaly", **a})
                enough = bool(parsed.get("enough", True))
                missing = [str(m) for m in parsed.get("missing", [])] if isinstance(parsed.get("missing"), list) else []
                return analysis, evidence, enough, missing
        except Exception as exc:
            logger.warning("finance.analyze.fallback", error=str(exc))
        analysis = [raw[:2000]] if raw else ["LLM 分析失败，无可用结论。"]
        return analysis, [{"type": "analysis", "summary": analysis[0], "raw": raw[:2000]}], True, []

    def _build_result(self, task, observations, sql_history, analysis, evidence) -> dict[str, Any]:
        """组装 Finance Result。"""
        if not analysis:
            analysis = ["未产生分析结论（数据不足）。"]
        return {
            "agent": "finance",
            "task": task,
            "summary": analysis[0],
            "analysis": analysis,
            "metrics": _collect_metrics(evidence),
            "anomalies": _collect_anomalies(evidence),
            "evidence": evidence,
            "observations": observations,
            "sql_history": sql_history,
            "confidence": 0.9 if observations else 0.1,
            "mode": "llm",
        }


# 供 graph.py 复用的 state 构建辅助
def initial_state(task: str, context: Optional[dict[str, Any]] = None) -> FinanceState:
    """构造 FinanceState 初始值。"""
    return {
        "task": task,
        "messages": [],
        "iteration": 0,
        "tool_calls": [],
        "sql_history": [],
        "observations": [],
        "analysis": [],
        "evidence": [],
        "final_result": None,
        "error": None,
        "sql_retry_count": 0,
        "tool_retry_count": 0,
        "max_iterations": settings.MAX_AGENT_ITERATIONS,
        "finance_context": context or {},
        "plan": [],
        "queried": set(),
        "enough": False,
        "missing": [],
    }
