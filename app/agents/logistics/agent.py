"""Logistics Agent 主逻辑（设计文档 11 节物流域）。

继承 OperationAgent，复用内部循环（plan→query→analyze→retry），
仅覆盖数据域白名单、优先级表、数据字典和 Prompt。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.agents.logistics.prompts import ANALYSIS_PROMPT, PLAN_PROMPT
from app.agents.logistics.state import LogisticsState
from app.agents.logistics.tools import get_logistics_tool_map
from app.agents.operation.agent import (
    OperationAgent,
    _collect_anomalies,
    _collect_metrics,
    _parse_analysis_json,
)
from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("logistics_agent")

# 物流数据域白名单
_KNOWN_REQS: frozenset[str] = frozenset({"inventory_risk", "stock_level", "inbound", "logistics_cost", "delivery"})


class LogisticsAgent(OperationAgent):
    """物流 Agent：库存/在途/缺货风险/物流成本/配送时效分析（LLM 模式）。"""

    # 各数据域的核心表（优先注入 schema 上下文）
    _REQ_PRIORITY_TABLES: dict[str, list[str]] = {
        "inventory_risk": ["mart_inventory_risk", "inventory_daily", "product_skus", "brands"],
        "stock_level": ["inventory", "warehouses", "product_skus"],
        "inbound": ["inbound_shipments", "warehouses", "product_skus"],
        "logistics_cost": ["logistics_cost", "carriers", "orders"],
        "delivery": ["logistics_orders", "tracking_events", "carriers"],
    }

    def __init__(self, model=None, executor=None):
        from app.llm import get_chat_model, llm_available
        if not llm_available():
            raise RuntimeError("未配置 LLM API Key。Logistics Agent 需要 LLM。")
        self.llm_ready = True
        self.model = model if model is not None else get_chat_model(tier="medium")
        self.tools = get_logistics_tool_map()
        self.executor = executor or self.tools["execute_readonly_sql"]
        self.max_iterations = settings.MAX_AGENT_ITERATIONS
        self.max_sql_retries = settings.MAX_SQL_RETRIES

    def _plan(self, task: str, context: dict[str, Any]) -> list[str]:
        """LLM 规划物流数据需求。"""
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            resp = self.model.invoke([
                SystemMessage(content="你是物流分析 Agent 的规划器，只输出数据域名称列表。"),
                HumanMessage(content=PLAN_PROMPT.format(task=task)),
            ])
            plan = [ln.strip().lower() for ln in str(resp.content).splitlines() if ln.strip()]
            plan = [p for p in plan if p in _KNOWN_REQS]
            if "inventory_risk" not in plan:
                plan.insert(0, "inventory_risk")
            return plan
        except Exception as exc:
            logger.warning("logistics.plan.fallback", error=str(exc))
            return ["inventory_risk"]

    def _build_schema_context(self, req: str, base: dict[str, Any]) -> dict[str, Any]:
        """构建 schema 上下文 + 物流数据字典。"""
        keyword = {
            "inventory_risk": "inventory",
            "stock_level": "inventory",
            "inbound": "inbound",
            "logistics_cost": "cost",
            "delivery": "tracking",
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
            logger.warning("logistics.schema_context.fail", error=str(exc))
            tables, metrics, dictionary = [], [], ""
        return {**base, "tables": tables, "metrics": metrics, "dictionary": dictionary}

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
            "⑤在途补货查 inbound_shipments（status=in_transit）。"
        )
        text = "\n".join(lines)
        logger.debug("logistics.dictionary", text=text[:1500])
        return text

    def _analyze(self, task: str, observations: list[dict[str, Any]]):
        """LLM 物流分析。"""
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
            logger.debug("logistics.analyze.llm", raw=raw[:2000])
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
            logger.warning("logistics.analyze.fallback", error=str(exc))
        analysis = [raw[:2000]] if raw else ["LLM 分析失败，无可用结论。"]
        return analysis, [{"type": "analysis", "summary": analysis[0], "raw": raw[:2000]}], True, []

    def _build_result(self, task, observations, sql_history, analysis, evidence) -> dict[str, Any]:
        """组装 Logistics Result。"""
        if not analysis:
            analysis = ["未产生分析结论（数据不足）。"]
        return {
            "agent": "logistics",
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
