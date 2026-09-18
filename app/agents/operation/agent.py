"""Operation Agent 主逻辑（设计文档 11 节）。

继承 BaseDepartmentAgent，只声明配置 + 实现数据字典。
内部循环（plan→query→analyze→retry）由基类提供。
"""

from __future__ import annotations

from typing import Any, Optional

from app.agents.base import (
    BaseDepartmentAgent,
    collect_anomalies,
    collect_metrics,
    parse_analysis_json,
)
from app.agents.operation.prompts import ANALYSIS_PROMPT, PLAN_PROMPT
from app.agents.operation.state import OperationState
from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("operation_agent")

# 已知数据域白名单（供 graph.py retry 过滤用）
_KNOWN_REQS: frozenset[str] = frozenset({"sales_sku", "brand_summary", "ad", "review", "inventory"})


class OperationAgent(BaseDepartmentAgent):
    """运营 Agent：怎么卖、卖得怎么样（LLM 模式）。"""

    AGENT_NAME = "operation"
    KNOWN_REQS = _KNOWN_REQS
    FALLBACK_REQ = "sales_sku"
    PLAN_PROMPT = PLAN_PROMPT
    ANALYSIS_PROMPT = ANALYSIS_PROMPT

    PRIORITY_TABLES: dict[str, list[str]] = {
        "sales_sku": ["mart_sales_daily", "sales_daily", "product_skus", "brands"],
        "brand_summary": ["mart_sales_daily", "brands"],
        "ad": ["ad_performance_daily", "ad_campaigns", "product_skus", "brands"],
        "review": ["reviews", "product_skus", "brands"],
        "inventory": ["inventory_daily", "product_skus", "brands"],
    }

    KEYWORD_MAP: dict[str, str] = {
        "sales_sku": "sku",
        "brand_summary": "brand",
        "ad": "campaign",
        "review": "review",
        "inventory": "inventory",
    }

    def _load_dictionary(self) -> str:
        """加载运营数据字典（品牌/市场/SKU/时间窗口/查询提示）。"""
        lines: list[str] = []
        try:
            r = self.executor("SELECT id, name FROM brands ORDER BY id LIMIT 20")
            lines.append("品牌: " + ", ".join(f"{row['id']}={row['name']}" for row in r["rows"]))
        except Exception:
            pass
        try:
            r = self.executor("SELECT DISTINCT country FROM sales_daily ORDER BY 1 LIMIT 20")
            lines.append("市场取值: " + ", ".join(str(row["country"]) for row in r["rows"]))
        except Exception:
            pass
        try:
            r = self.executor("SELECT sku_code FROM product_skus ORDER BY id LIMIT 10")
            lines.append("SKU 编码样例: " + ", ".join(str(row["sku_code"]) for row in r["rows"]) + "（SKU 不含品牌名）")
        except Exception:
            pass
        try:
            r = self.executor("SELECT MIN(date) AS d0, MAX(date) AS d1 FROM sales_daily")
            row = r["rows"][0] if r["rows"] else {}
            lines.append(f"数据时间窗口: {row.get('d0')} ~ {row.get('d1')}（共约 90 天；last21 = 最后21天）")
        except Exception:
            pass
        lines.append(
            "查询提示: ①品牌过滤用 JOIN brands 按 name 匹配（如 brands.name='SweetNight'），"
            "不要用 sku_code 匹配品牌名；②时间分段用 date > (SELECT MAX(date) - 21 FROM 主表) 区分 last21/prev；"
            "③prev 段约 69 天、last21 段约 21 天，天数不同——对比必须换算为日均"
            "（如 SUM(units)/COUNT(DISTINCT date) 输出 daily_units），禁止直接比较两段总量；"
            "④聚合查询应能返回行，若过滤过严返回空请放宽条件。"
        )
        text = "\n".join(lines)
        logger.debug("operation.dictionary", text=text[:1500])
        return text


# 向后兼容别名（graph.py 等旧导入路径）
_parse_analysis_json = parse_analysis_json
_collect_metrics = collect_metrics
_collect_anomalies = collect_anomalies


# 供 graph.py 复用的 state 构建辅助
def initial_state(task: str, context: Optional[dict[str, Any]] = None) -> OperationState:
    """构造 OperationState 初始值。"""
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
        "sales_context": context or {},
        "plan": [],
        "queried": set(),
        "enough": False,
        "missing": [],
    }
