"""Finance Agent State（设计文档 12 节）。"""

from typing import Any, TypedDict

from app.graph.state import DepartmentState


class FinanceState(DepartmentState):
    """财务 Agent 内部 State。"""

    # 财务专用上下文
    finance_context: dict[str, Any]

    # SubGraph 内部流转字段
    plan: list[str]
    queried: set[str]
    enough: bool
    missing: list[str]
