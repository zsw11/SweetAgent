"""Finance Agent 工具注册（复用 Operation 的 SQL Tool System）。"""

from app.agents.operation.tools import get_operation_tool_map


def get_finance_tools():
    """返回 Finance Agent 可用的工具列表（与 Operation 共享同一套 SQL 工具）。"""
    from app.agents.operation.tools import get_operation_tools
    return get_operation_tools()


def get_finance_tool_map():
    """返回 {工具名: 可调用对象}，供 FinanceAgent 内部按名调用。"""
    return get_operation_tool_map()
