"""Finance Agent 主逻辑（设计文档 12 节）。

继承 BaseDepartmentAgent，只声明配置 + 实现数据字典。
"""

from __future__ import annotations

from typing import Any, Optional

from app.agents.base import BaseDepartmentAgent
from app.agents.finance.prompts import ANALYSIS_PROMPT, PLAN_PROMPT
from app.agents.finance.state import FinanceState
from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("finance_agent")

_KNOWN_REQS: frozenset[str] = frozenset({"profit", "cost", "revenue", "refund", "platform_fee", "knowledge"})


class FinanceAgent(BaseDepartmentAgent):
    """财务 Agent：利润/成本/费用/退款分析（LLM 模式）。"""

    AGENT_NAME = "finance"
    KNOWN_REQS = _KNOWN_REQS
    FALLBACK_REQ = "profit"
    PLAN_PROMPT = PLAN_PROMPT
    ANALYSIS_PROMPT = ANALYSIS_PROMPT
    KNOWLEDGE_DEPARTMENT = "finance"

    PRIORITY_TABLES: dict[str, list[str]] = {
        "profit": ["mart_product_profit_daily", "profit_daily", "product_skus", "brands"],
        "cost": ["cost_daily", "financial_transactions", "stores"],
        "revenue": ["revenue_daily", "financial_transactions", "stores"],
        "refund": ["refunds", "orders", "product_skus"],
        "platform_fee": ["platform_fees", "orders", "stores"],
        "knowledge": ["knowledge_documents", "knowledge_chunks"],
    }

    KEYWORD_MAP: dict[str, str] = {
        "profit": "profit",
        "cost": "cost",
        "revenue": "revenue",
        "refund": "refund",
        "platform_fee": "fee",
        "knowledge": "knowledge",
    }

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
            "logistics_cost/refund_cost/gross_profit/contribution_profit/profit_margin，优先用此宽表；"
            "⑤财务口径（收入/贡献毛利/退款规则）用 knowledge 数据域（RAG 向量检索）。"
        )
        text = "\n".join(lines)
        logger.debug("finance.dictionary", text=text[:1500])
        return text


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
