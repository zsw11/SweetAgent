"""Finance Agent State（设计文档 9 节）。"""

from typing import Any, TypedDict

from app.graph.state import DepartmentState


class FinanceState(DepartmentState):
    """财务 Agent 内部 State。"""

    finance_context: dict[str, Any]
    profit_context: dict[str, Any]
