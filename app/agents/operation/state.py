"""Operation Agent State（设计文档 9 节）。

在 DepartmentState 基础上扩展运营专用字段。
"""

from typing import Any, TypedDict

from app.graph.state import DepartmentState


class OperationState(DepartmentState):
    """运营 Agent 内部 State。"""

    # 运营专用上下文
    sales_context: dict[str, Any]
    ad_context: dict[str, Any]
    traffic_context: dict[str, Any]

    # SubGraph 内部流转字段
    # 待查清单（retry只追加）
    plan: list[str]
    # ** 已查记忆 **（查过就永久记录，防重复查询）
    queried: set[str]
    enough: bool
    missing: list[str]
