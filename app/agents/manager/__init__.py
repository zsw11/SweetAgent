"""Manager Agent：问题理解、任务拆解、依赖 DAG 规划（设计文档 4、6 节）。

Manager 负责"做事之前"：理解问题 -> 确定目标 -> 判断涉及部门 -> 拆解任务 -> 建立依赖关系。
不负责最终业务结论（Decision Agent 职责）。
"""

from app.agents.manager.agent import ManagerAgent, initial_state

__all__ = ["ManagerAgent", "initial_state"]
