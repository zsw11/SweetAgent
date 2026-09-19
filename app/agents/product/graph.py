"""Product Agent SubGraph（复用 BaseDepartmentAgent 内部循环模式）。

    START -> plan -> query -> analyze -> decide --(不足且可重试)--> query
                                       |--(足够)--> END

cross_department_context（Operation/Finance/Logistics 结果）由 Manager 按依赖 DAG 注入，
Product 只查自己的数据域（product/lifecycle/development/consumer/market），
不重复查询销售/利润/库存（设计文档 6 节）。
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from app.agents.product.agent import ProductAgent, initial_state, _KNOWN_REQS
from app.agents.product.state import ProductState
from app.observability.logging import get_logger

logger = get_logger("product_graph")


def build_product_agent(agent: Optional[ProductAgent] = None):
    """构建并编译 Product Agent SubGraph。"""
    ag = agent or ProductAgent()

    def _plan(state: dict[str, Any]) -> dict[str, Any]:
        task = state.get("task", "")
        context = state.get("cross_department_context") or {}
        plan = ag._plan(task, context)
        return {"plan": plan, "iteration": state.get("iteration", 0) + 1}

    def _query(state: dict[str, Any]) -> dict[str, Any]:
        task = state.get("task", "")
        context = state.get("cross_department_context") or {}
        plan = list(state.get("plan") or [])
        queried = set(state.get("queried") or [])
        observations = list(state.get("observations") or [])
        sql_history = list(state.get("sql_history") or [])

        for req in plan:
            if req in queried:
                continue
            try:
                obs = ag._query_one(req, task, context)
                observations.append(obs)
                sql_history.append({"requirement": req, "sql": obs.get("sql"), "ok": True})
                queried.add(req)
            except Exception as exc:
                logger.warning("product_graph.query.fail", requirement=req, error=str(exc))
                sql_history.append({"requirement": req, "ok": False, "error": str(exc)})
                queried.add(req)

        logger.info("product_graph.query.batch_done", ok=len([h for h in sql_history if h.get("ok")]), failed=len([h for h in sql_history if not h.get("ok")]))
        return {
            "observations": observations,
            "sql_history": sql_history,
            "queried": queried,
        }

    def _route_query(state: dict[str, Any]) -> str:
        if state.get("enough"):
            return "done"
        return "analyze"

    def _analyze(state: dict[str, Any]) -> dict[str, Any]:
        task = state.get("task", "")
        observations = list(state.get("observations") or [])
        context = state.get("cross_department_context") or {}
        analysis, evidence, enough, missing = ag._analyze(task, observations, context)
        final_result = ag._build_result(
            task, observations, list(state.get("sql_history") or []), analysis, evidence
        )
        return {"analysis": analysis, "evidence": evidence, "final_result": final_result, "enough": enough, "missing": missing}

    def _decide(state: dict[str, Any]) -> str:
        if state.get("enough") or state.get("iteration", 0) >= ag.max_iterations:
            return "done"
        return "retry"

    def _retry(state: dict[str, Any]) -> dict[str, Any]:
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

    builder = StateGraph(ProductState)
    builder.add_node("plan", _plan)
    builder.add_node("query", _query)
    builder.add_node("analyze", _analyze)
    builder.add_node("retry", _retry)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "query")
    builder.add_conditional_edges("query", _route_query, {"analyze": "analyze", "done": END})
    builder.add_conditional_edges("analyze", _decide, {"done": END, "retry": "retry"})
    builder.add_edge("retry", "query")

    return builder.compile()


def run_product(task: str, context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """便捷入口：直接运行 Product SubGraph，返回 final_result。

    Args:
        task: 产品策略任务描述。
        context: 跨部门上下文（Operation/Finance/Logistics 结论摘要）。
    """
    app = build_product_agent(ProductAgent(cross_context=context))
    result = app.invoke(initial_state(task, context))
    return result.get("final_result") or {"agent": "product", "error": "no result"}
