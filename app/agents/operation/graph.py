"""Operation Agent SubGraph（设计文档 11 节内部循环 / Phase 3 Agent 结构）。

用 LangGraph StateGraph 实现内部循环：

    START -> plan -> query --(还有需求)--> query
                        |--(需求完成)--> analyze -> decide --(不足且可重试)--> query
                                                          --(足够)--> END

节点逻辑复用 OperationAgent 的内部方法（agent.py）。编译后的 SubGraph 可嵌入主 Graph（Manager 调度）。
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from app.agents.operation.agent import (
    OperationAgent,
    _KNOWN_REQS,
    initial_state,
)
from app.agents.operation.state import OperationState
from app.observability.logging import get_logger

logger = get_logger("operation_graph")


def build_operation_agent(agent: Optional[OperationAgent] = None):
    """构建并编译 Operation Agent SubGraph。

    Args:
        agent: 可注入自定义 OperationAgent（测试用）；默认自动创建。

    Returns:
        编译后的 StateGraph（invoke 传入 OperationState 字典）。
    """
    ag = agent or OperationAgent()

    def _plan(state: dict[str, Any]) -> dict[str, Any]:
        task = state.get("task", "")
        context = state.get("sales_context") or {}
        plan = ag._plan(task, context)
        return {"plan": plan, "iteration": state.get("iteration", 0) + 1}

    def _query(state: dict[str, Any]) -> dict[str, Any]:
        task = state.get("task", "")
        context = state.get("sales_context") or {}
        plan = list(state.get("plan") or [])
        queried = set(state.get("queried") or [])
        observations = list(state.get("observations") or [])
        sql_history = list(state.get("sql_history") or [])

        # 批量执行所有未查询需求（避免逐需求多轮图调度）。
        # 每个需求只查一次：queried 是"已查记忆"，retry 追加的新需求下次进入本节点时才会被查。
        ok_count, fail_count = 0, 0
        for req in plan:
            if req in queried:
                continue
            try:
                obs = ag._query_one(req, task, context)
                observations.append(obs)
                sql_history.append({"requirement": req, "sql": obs.get("sql"), "ok": True})
                queried.add(req)
                ok_count += 1
            except Exception as exc:
                logger.warning("operation_graph.query.fail", requirement=req, error=str(exc))
                sql_history.append({"requirement": req, "ok": False, "error": str(exc)})
                queried.add(req)
                fail_count += 1
        logger.debug("operation_graph.query.batch_done", ok=ok_count, fail=fail_count)

        return {
            "observations": observations,
            "sql_history": sql_history,
            "queried": queried,
        }

    def _route_query(state: dict[str, Any]) -> str:
        # query 已批量查完所有未查需求。若 retry 已标记无可补数据（enough=True）则直接结束，
        # 避免 analyze 空转（曾出现 analyze 被重复调用 8 次的 bug）；否则进 analyze 判断是否需要补数据。
        if state.get("enough"):
            return "done"
        return "analyze"

    def _analyze(state: dict[str, Any]) -> dict[str, Any]:
        task = state.get("task", "")
        observations = list(state.get("observations") or [])
        analysis, evidence, enough, missing = ag._analyze(task, observations)
        final_result = ag._build_result(task, observations, list(state.get("sql_history") or []), analysis, evidence)
        return {"analysis": analysis, "evidence": evidence, "final_result": final_result, "enough": enough, "missing": missing}

    def _decide(state: dict[str, Any]) -> str:
        if state.get("enough") or state.get("iteration", 0) >= ag.max_iterations:
            return "done"
        return "retry"

    def _retry(state: dict[str, Any]) -> dict[str, Any]:
        """补需求：仅追加"已知且未查询过"的缺失项。

        若 missing 已全在 plan/queried（或都是未知需求），说明没有可补的数据，
        标记 enough=True 直接结束，避免 analyze->retry 死循环空转（LLM 每轮都真实调用）。
        """
        plan = list(state.get("plan") or [])
        queried = set(state.get("queried") or [])
        added = False
        for m in state.get("missing") or []:
            if m not in plan and m not in queried and m in _KNOWN_REQS:
                plan.append(m)
                added = True
        out: dict[str, Any] = {"plan": plan, "iteration": state.get("iteration", 0) + 1}
        if not added:
            out["enough"] = True
        return out

    builder = StateGraph(OperationState)
    builder.add_node("plan", _plan)
    builder.add_node("query", _query)
    builder.add_node("analyze", _analyze)
    builder.add_node("retry", _retry)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "query")
    builder.add_conditional_edges(
        "query",
        _route_query,
        {"analyze": "analyze", "done": END},
    )
    # analyze 后由 _decide 路由：足够 -> END；不足且可重试 -> retry（补需求后重新查询）
    builder.add_conditional_edges("analyze", _decide, {"done": END, "retry": "retry"})
    builder.add_edge("retry", "query")

    return builder.compile()


def run_operation(task: str, context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """便捷入口：直接运行 Operation SubGraph，返回 final_result。"""
    app = build_operation_agent()
    result = app.invoke(initial_state(task, context))
    return result.get("final_result") or {"agent": "operation", "error": "no result"}
