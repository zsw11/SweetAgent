"""Logistics Agent State（设计文档 9 节）。"""

from typing import Any, TypedDict

from app.graph.state import DepartmentState


class LogisticsState(DepartmentState):
    """物流 Agent 内部 State。"""

    inventory_context: dict[str, Any]
    logistics_context: dict[str, Any]
