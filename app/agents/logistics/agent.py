"""Logistics Agent 主逻辑（设计文档 11 节物流域）。

继承 BaseDepartmentAgent，只声明配置 + 实现数据字典。
"""

from __future__ import annotations

from typing import Any, Optional

from app.agents.base import BaseDepartmentAgent
from app.agents.logistics.prompts import ANALYSIS_PROMPT, PLAN_PROMPT
from app.agents.logistics.state import LogisticsState
from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("logistics_agent")

_KNOWN_REQS: frozenset[str] = frozenset({"inventory_risk", "stock_level", "inbound", "logistics_cost", "delivery", "knowledge"})


class LogisticsAgent(BaseDepartmentAgent):
    """物流 Agent：库存/在途/缺货风险/物流成本/配送时效分析（LLM 模式）。"""

    AGENT_NAME = "logistics"
    KNOWN_REQS = _KNOWN_REQS
    FALLBACK_REQ = "inventory_risk"
    PLAN_PROMPT = PLAN_PROMPT
    ANALYSIS_PROMPT = ANALYSIS_PROMPT
    KNOWLEDGE_DEPARTMENT = "logistics"

    PRIORITY_TABLES: dict[str, list[str]] = {
        "inventory_risk": ["mart_inventory_risk", "inventory_daily", "product_skus", "brands"],
        "stock_level": ["inventory", "warehouses", "product_skus"],
        "inbound": ["inbound_shipments", "warehouses", "product_skus"],
        "logistics_cost": ["logistics_cost", "carriers", "orders"],
        "delivery": ["logistics_orders", "tracking_events", "carriers"],
        "knowledge": ["knowledge_documents", "knowledge_chunks"],
    }

    KEYWORD_MAP: dict[str, str] = {
        "inventory_risk": "inventory",
        "stock_level": "inventory",
        "inbound": "inbound",
        "logistics_cost": "cost",
        "delivery": "tracking",
        "knowledge": "knowledge",
    }

    def _load_dictionary(self) -> str:
        """加载物流数据字典。"""
        lines: list[str] = []
        try:
            r = self.executor("SELECT id, code, name, country_code FROM warehouses ORDER BY id LIMIT 20")
            lines.append("仓库: " + ", ".join(f"{row['id']}={row['code']}({row['country_code']})" for row in r["rows"]))
        except Exception:
            pass
        try:
            r = self.executor("SELECT id, code, name FROM carriers ORDER BY id LIMIT 20")
            lines.append("承运商: " + ", ".join(f"{row['id']}={row['code']}" for row in r["rows"]))
        except Exception:
            pass
        try:
            r = self.executor("SELECT id, name FROM brands ORDER BY id LIMIT 20")
            lines.append("品牌: " + ", ".join(f"{row['id']}={row['name']}" for row in r["rows"]))
        except Exception:
            pass
        try:
            r = self.executor("SELECT MIN(date) AS d0, MAX(date) AS d1 FROM mart_inventory_risk")
            row = r["rows"][0] if r["rows"] else {}
            lines.append(f"库存数据时间窗口: {row.get('d0')} ~ {row.get('d1')}")
        except Exception:
            pass
        lines.append(
            "查询提示: ①品牌过滤用 JOIN brands 按 name 匹配；"
            "②库存风险宽表 mart_inventory_risk 已含 stock_days/forecast_demand/risk_level，优先用此表；"
            "③stock_days < 12 为高风险；④实时库存查 inventory 表，历史趋势查 inventory_daily；"
            "⑤在途补货查 inbound_shipments（status=in_transit）；"
            "⑥物流规则（SLA/库存阈值/补货）用 knowledge 数据域（RAG 向量检索）。"
        )
        text = "\n".join(lines)
        logger.debug("logistics.dictionary", text=text[:1500])
        return text


def initial_state(task: str, context: Optional[dict[str, Any]] = None) -> LogisticsState:
    """构造 LogisticsState 初始值。"""
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
        "logistics_context": context or {},
        "plan": [],
        "queried": set(),
        "enough": False,
        "missing": [],
    }
