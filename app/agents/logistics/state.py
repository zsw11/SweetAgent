"""Logistics Agent State（设计文档 12 节）。"""

from typing import Any, TypedDict

from app.graph.state import DepartmentState


class LogisticsState(DepartmentState):
    """物流 Agent 内部 State。"""

    # 物流专用上下文
    logistics_context: dict[str, Any]

    # SubGraph 内部流转字段
    plan: list[str]
    queried: set[str]
    enough: bool
    missing: list[str]
