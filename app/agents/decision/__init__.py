"""Decision Agent：跨部门结果汇总、因果分析、决策建议（设计文档 15-16 节）。

不负责大量查数据，接收各部门 Result 后做事实整合 -> 交叉验证 -> 冲突检测 ->
原因分析 -> 影响评估 -> 方案制定 -> 优先级排序 -> 最终报告。
"""

from app.agents.decision.agent import DecisionAgent, initial_state
from app.agents.decision.graph import build_decision_agent, run_decision

__all__ = ["DecisionAgent", "initial_state", "build_decision_agent", "run_decision"]
