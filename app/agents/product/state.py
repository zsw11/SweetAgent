"""Product Agent State（设计文档 9 节）。

cross_department_context 由 Manager 按依赖 DAG 注入
（Operation / Finance / Logistics 结果），禁止 Product 自由调用其他部门 Agent。
"""

from typing import Any, TypedDict

from app.graph.state import DepartmentState


class ProductState(DepartmentState):
    """产品策略 Agent 内部 State。"""

    market_context: dict[str, Any]
    consumer_context: dict[str, Any]
    competitor_context: dict[str, Any]
    cross_department_context: dict[str, Any]
    product_plan: dict[str, Any]

    # SubGraph 内部流转字段（plan->query->analyze->retry）
    plan: list[str]
    queried: set[str]
    enough: bool
    missing: list[str]
