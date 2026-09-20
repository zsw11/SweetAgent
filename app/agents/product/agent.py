"""Product Strategy Agent 主逻辑（设计文档 14 节）。

继承 BaseDepartmentAgent，只声明配置 + 实现数据字典。
跨部门上下文（Operation/Finance/Logistics 结果）由 Manager 按依赖 DAG 注入
（cross_context），Product 不自由调用其他部门 Agent（设计文档 5.1 / 6 节）。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.agents.base import BaseDepartmentAgent
from app.agents.product.prompts import ANALYSIS_PROMPT, PLAN_PROMPT
from app.agents.product.state import ProductState
from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("product_agent")

# 已知数据域白名单（供 graph.py retry 过滤用）。
# 注意：销售/利润/库存由 Operation/Finance/Logistics 查询，跨部门上下文注入，
# Product 不重复查询这些数据域（设计文档 6 节）。market = 知识库文档清单；
# knowledge = RAG 向量检索（知识库内容），两者职责分离。
_KNOWN_REQS: frozenset[str] = frozenset({"product", "lifecycle", "development", "consumer", "market", "knowledge"})


class ProductAgent(BaseDepartmentAgent):
    """产品策略 Agent：下一阶段开发什么产品（LLM 模式，跨部门上下文注入）。"""

    AGENT_NAME = "product"
    KNOWN_REQS = _KNOWN_REQS
    FALLBACK_REQ = "product"
    PLAN_PROMPT = PLAN_PROMPT
    ANALYSIS_PROMPT = ANALYSIS_PROMPT
    KNOWLEDGE_DEPARTMENT = "product"

    PRIORITY_TABLES: dict[str, list[str]] = {
        "product": ["products", "product_skus", "product_categories", "product_prices"],
        "lifecycle": ["product_lifecycle", "product_skus"],
        "development": ["product_development_projects", "brands"],
        "consumer": ["reviews", "review_aspects", "customer_feedback", "return_reasons"],
        "market": ["knowledge_documents", "knowledge_chunks"],
        "knowledge": ["knowledge_documents", "knowledge_chunks"],
    }

    KEYWORD_MAP: dict[str, str] = {
        "product": "product",
        "lifecycle": "lifecycle",
        "development": "development_project",
        "consumer": "review",
        "market": "knowledge",
        "knowledge": "knowledge",
    }

    def __init__(self, model=None, executor=None, cross_context: Optional[dict[str, Any]] = None):
        super().__init__(model=model, executor=executor)
        # 跨部门上下文（Operation/Finance/Logistics 结论摘要），由主 Graph 注入
        self.cross_context: dict[str, Any] = cross_context or {}

    def _load_dictionary(self) -> str:
        """加载产品数据字典（品牌/商品/SKU/生命周期/开发项目/知识库/跨部门结论）。"""
        lines: list[str] = []
        try:
            r = self.executor("SELECT id, name FROM brands ORDER BY id LIMIT 20")
            lines.append("品牌: " + ", ".join(f"{row['id']}={row['name']}" for row in r["rows"]))
        except Exception:
            pass
        try:
            r = self.executor(
                "SELECT p.id, p.product_name, p.product_type, p.launch_date, "
                "s.sku_code, s.country_code, s.sale_price, s.cost "
                "FROM products p JOIN product_skus s ON s.product_id = p.id ORDER BY p.id LIMIT 30"
            )
            rows = r["rows"]
            lines.append(
                f"商品与SKU（{len(rows)} 条，前 12 条样例）: " + "; ".join(
                    f"{row['sku_code']}={row['product_name']}"
                    f"({row['country_code']}, 售价{row['sale_price']}, 成本{row['cost']}, "
                    f"毛利率{(row['sale_price'] - row['cost']) / row['sale_price'] * 100:.1f}%)"
                    for row in rows[:12]
                )
            )
        except Exception:
            pass
        try:
            r = self.executor(
                "SELECT s.sku_code, l.stage, l.start_date FROM product_lifecycle l "
                "JOIN product_skus s ON s.id = l.sku_id ORDER BY l.start_date LIMIT 20"
            )
            lines.append("生命周期: " + ", ".join(f"{row['sku_code']}={row['stage']}" for row in r["rows"]))
        except Exception:
            pass
        try:
            r = self.executor(
                "SELECT name, status, stage, planned_launch_date "
                "FROM product_development_projects ORDER BY id LIMIT 20"
            )
            lines.append("开发项目: " + "; ".join(
                f"{row['name']}({row['status']}/{row['stage']}, 计划上架{row.get('planned_launch_date')})"
                for row in r["rows"]
            ))
        except Exception:
            pass
        try:
            r = self.executor(
                "SELECT title, source_type, department, market FROM knowledge_documents "
                "WHERE status='active' ORDER BY id LIMIT 20"
            )
            lines.append("知识库文档: " + "; ".join(
                f"{row['title']}({row['source_type']}/{row['department']})" for row in r["rows"]
            ))
        except Exception:
            pass
        if self.cross_context:
            lines.append(
                "跨部门结论（Operation/Finance/Logistics 已查，勿重复查询销售/利润/库存）: "
                + json.dumps(self.cross_context, ensure_ascii=False, default=str)[:2000]
            )
        lines.append(
            "查询提示: ①品牌过滤用 JOIN brands 按 name 匹配；"
            "②知识库内容检索请使用 knowledge 数据域（RAG 向量检索，自动返回最相关片段，"
            "覆盖行业趋势/竞品/规格/SOP）；market 域仅查询文档清单 knowledge_documents.title；"
            "③评论按 SKU 聚合评分与情感（review_aspects 有 aspect/sentiment），SN-Q12-US 有塌陷类负面评论埋点；"
            "④新品建议参考开发项目 product_development_projects 与知识库 SOP（目标毛利率不低于 35%）；"
            "⑤售价/成本来自 product_skus，毛利率=(sale_price-cost)/sale_price。"
        )
        text = "\n".join(lines)
        logger.debug("product.dictionary", text=text[:1500])
        return text


def initial_state(task: str, context: Optional[dict[str, Any]] = None) -> ProductState:
    """构造 ProductState 初始值（context 为跨部门上下文，Manager 按依赖 DAG 注入）。"""
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
        "market_context": context or {},
        "consumer_context": context or {},
        "competitor_context": context or {},
        "cross_department_context": context or {},
        "product_plan": {},
        "plan": [],
        "queried": set(),
        "enough": False,
        "missing": [],
    }
