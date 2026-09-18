"""Manager State。

Manager 的规划结果直接写入 GlobalState（task_plan / required_agents / current_stage），
此处保留本 Agent 自身的中间状态。
"""

from typing import TypedDict


class ManagerState(TypedDict, total=False):
    user_question: str
    intent: str
    task_plan: dict
    required_agents: list[str]
    missing_info: list[str]  # 需要向用户确认的信息
    planning_errors: list[str]
