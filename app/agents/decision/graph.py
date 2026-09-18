"""Decision Agent SubGraph（设计文档 15 节）。

Decision 是一次性综合分析，不需要内部循环（不像 Operation 需要 plan->query->analyze 闭环）。

    START -> synthesize -> END

synthesize 节点：接收 user_question + department_results，调用 DecisionAgent 生成结构化 final_report。
编译后的 SubGraph 可嵌入主 Graph（Manager 调度所有部门完成后进入 Decision）。
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from app.agents.decision.agent import DecisionAgent, initial_state
from app.agents.decision.state import DecisionState
from app.observability.logging import get_logger

logger = get_logger("decision_graph")


def build_decision_agent(agent: Optional[DecisionAgent] = None):
    """构建并编译 Decision Agent SubGraph。

    Args:
        agent: 可注入自定义 DecisionAgent（测试用）；默认自动创建。

    Returns:
        编译后的 StateGraph（invoke 传入 DecisionState 字典）。
    """
    ag = agent or DecisionAgent()

    def _synthesize(state: dict[str, Any]) -> dict[str, Any]:
        """综合各部门结果，生成结构化决策报告。"""
        user_question = state.get("user_question", "")
        department_results = state.get("department_results") or {}
        try:
            report = ag.run(user_question, department_results)
            return {
                "final_report": report,
                "root_causes": report.get("root_causes", []),
                "recommendations": report.get("recommendations", []),
                "confidence": report.get("confidence", 0.0),
                "evidence": report.get("findings", []),
            }
        except Exception as exc:
            logger.error("decision_graph.synthesize.fail", error=str(exc))
            return {
                "final_report": {
                    "summary": f"Decision Agent 执行失败: {exc}",
                    "findings": [],
                    "root_causes": [],
                    "recommendations": [],
                    "risks": [{"risk": str(exc), "severity": "high"}],
                    "confidence": 0.0,
                },
                "confidence": 0.0,
            }

    builder = StateGraph(DecisionState)
    builder.add_node("synthesize", _synthesize)
    builder.add_edge(START, "synthesize")
    builder.add_edge("synthesize", END)

    return builder.compile()


def run_decision(
    user_question: str,
    department_results: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """便捷入口：直接运行 Decision SubGraph，返回 final_report。"""
    app = build_decision_agent()
    result = app.invoke(initial_state(user_question, department_results))
    return result.get("final_report") or {"summary": "Decision Agent 未产生结果", "confidence": 0.0}
