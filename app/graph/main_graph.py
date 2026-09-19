"""主图：Parent Graph + Department SubGraph（设计文档 7 节 / 58 节最小闭环）。

执行流程（循环路由模式）：
  START -> manager -> router -> [department subgraph] -> router -> ... -> decision -> END

Phase 1 最小闭环：Manager -> Operation -> Decision（设计文档 58 节）。
后续 Phase 3 接入 finance/logistics/product 后，Router 自动调度（无需改主图结构）。
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from app.agents.decision import DecisionAgent
from app.agents.manager import ManagerAgent
from app.graph.router import make_department_node, route_fn, router_node
from app.graph.state import GlobalState
from app.observability.logging import get_logger

logger = get_logger("main_graph")


def build_main_graph(
    manager_agent: Optional[ManagerAgent] = None,
    decision_agent: Optional[DecisionAgent] = None,
):
    """构建并编译主 Graph。

    Args:
        manager_agent: 可注入自定义 ManagerAgent（测试用）。
        decision_agent: 可注入自定义 DecisionAgent（测试用）。

    Returns:
        编译后的 StateGraph（invoke 传入 GlobalState 字典）。
    """
    mgr = manager_agent or ManagerAgent()
    dec = decision_agent or DecisionAgent()

    # ------------------------------------------------------------------
    # 节点定义
    # ------------------------------------------------------------------

    def _manager(state: dict[str, Any]) -> dict[str, Any]:
        """Manager 节点：理解问题 -> 任务拆解 -> 依赖 DAG。"""
        user_question = state.get("user_question", "")
        logger.info("main.manager.start", question=user_question[:100])
        try:
            task_plan = mgr.run(user_question)
            logger.info(
                "main.manager.done",
                intent=task_plan.get("intent"),
                required_agents=task_plan.get("required_agents"),
            )
            return {
                "task_plan": task_plan,
                "required_agents": task_plan.get("required_agents", []),
                "current_stage": "running",
                "completed_tasks": [],
                "skipped_tasks": [],
                "current_task": "",
            }
        except Exception as exc:
            logger.error("main.manager.fail", error=str(exc))
            return {
                "error_state": {"node": "manager", "error": str(exc)},
                "current_stage": "error",
            }

    # 部门节点（operation / finance / logistics / product 均已实现）
    operation_node = make_department_node("operation")
    finance_node = make_department_node("finance")
    logistics_node = make_department_node("logistics")

    # Product 节点：注入跨部门上下文（Operation/Finance/Logistics 结论摘要，设计文档 6 节）
    def _product_context_builder(state: dict[str, Any]) -> Optional[dict[str, Any]]:
        """从 department_results 提取上游部门结论，作为 Product 的跨部门上下文。"""
        dept = state.get("department_results") or {}
        ctx: dict[str, Any] = {}
        for name in ("operation", "finance", "logistics"):
            r = dept.get(name)
            if isinstance(r, dict):
                ctx[name] = {
                    "summary": r.get("summary", ""),
                    "metrics": (r.get("metrics") or [])[:5],
                    "anomalies": (r.get("anomalies") or [])[:5],
                    "confidence": r.get("confidence", 0.0),
                }
        return ctx or None

    product_node = make_department_node("product", context_builder=_product_context_builder)

    def _decision(state: dict[str, Any]) -> dict[str, Any]:
        """Decision 节点：汇总各部门结果 -> 结构化最终报告。"""
        user_question = state.get("user_question", "")
        department_results = state.get("department_results") or {}
        skipped = state.get("skipped_tasks") or []
        if skipped:
            logger.info("main.decision.with_skipped", skipped=skipped)

        logger.info("main.decision.start", departments=list(department_results.keys()))
        try:
            report = dec.run(user_question, department_results)
            logger.info("main.decision.done", confidence=report.get("confidence"))
            return {
                "decision_result": report,
                "final_answer": report.get("summary", ""),
                "current_stage": "done",
            }
        except Exception as exc:
            logger.error("main.decision.fail", error=str(exc))
            return {
                "error_state": {"node": "decision", "error": str(exc)},
                "current_stage": "error",
                "decision_result": {
                    "summary": f"Decision Agent 执行失败: {exc}",
                    "confidence": 0.0,
                },
            }

    # ------------------------------------------------------------------
    # 图组装
    # ------------------------------------------------------------------
    builder = StateGraph(GlobalState)
    builder.add_node("manager", _manager)
    builder.add_node("router", router_node)
    builder.add_node("operation", operation_node)
    builder.add_node("finance", finance_node)
    builder.add_node("logistics", logistics_node)
    builder.add_node("product", product_node)
    builder.add_node("decision", _decision)

    builder.add_edge(START, "manager")
    builder.add_edge("manager", "router")

    # 路由条件边：route_fn 返回就绪 agent 列表（并行 fan-out 无依赖部门）；
    # 全部完成 -> decision；Product 依赖 O/F/L 自然落在后续批次（串行）
    builder.add_conditional_edges(
        "router",
        route_fn,
        {
            "operation": "operation",
            "finance": "finance",
            "logistics": "logistics",
            "product": "product",
            "decision": "decision",
        },
    )

    # 部门执行完 fan-in 回 router（LangGraph 等待本批并行分支全部完成后再调度 router）
    builder.add_edge("operation", "router")
    builder.add_edge("finance", "router")
    builder.add_edge("logistics", "router")
    builder.add_edge("product", "router")
    # decision 是终点
    builder.add_edge("decision", END)

    return builder.compile()


def run_question(
    question: str,
    thread_id: str = "default",
    user_id: str = "default",
) -> dict[str, Any]:
    """执行一次用户问题（主入口）。

    Args:
        question: 用户自然语言问题。
        thread_id: 会话 ID（用于 checkpoint / 记忆）。
        user_id: 用户 ID。

    Returns:
        包含 final_answer / decision_result / department_results 的完整状态字典。
    """
    logger.info("run_question.start", thread_id=thread_id, question=question[:100])
    app = build_main_graph()
    initial_state: GlobalState = {
        "thread_id": thread_id,
        "user_id": user_id,
        "user_question": question,
        "current_stage": "planning",
        "department_results": {},
        "completed_tasks": [],
        "skipped_tasks": [],
        "current_task": "",
    }
    result = app.invoke(initial_state)
    logger.info(
        "run_question.done",
        thread_id=thread_id,
        stage=result.get("current_stage"),
        confidence=result.get("decision_result", {}).get("confidence"),
    )
    return result
