"""Logistics Agent SubGraph（复用 Operation 的内部循环模式）。

    START -> plan -> query -> analyze -> decide --(不足且可重试)--> query
                                       |--(足够)--> END
"""

from __future__ import annotations

import concurrent.futures
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from app.agents.logistics.agent import LogisticsAgent, initial_state, _KNOWN_REQS
from app.agents.logistics.state import LogisticsState
from app.observability.logging import get_logger

logger = get_logger("logistics_graph")


def build_logistics_agent(agent: Optional[LogisticsAgent] = None):
    """构建并编译 Logistics Agent SubGraph。"""
    ag = agent or LogisticsAgent()

    def _plan(state: dict[str, Any]) -> dict[str, Any]:
        task = state.get("task", "")
        context = state.get("logistics_context") or {}
        plan = ag._plan(task, context)
        return {"plan": plan, "iteration": state.get("iteration", 0) + 1}

    def _query(state: dict[str, Any]) -> dict[str, Any]:
        task = state.get("task", "")
        context = state.get("logistics_context") or {}
        plan = list(state.get("plan") or [])
        queried = set(state.get("queried") or [])
        observations = list(state.get("observations") or [])
        sql_history = list(state.get("sql_history") or [])
        # 查询规划的所有数据域（OPT-05 通道就绪：无依赖数据域并行，耗时 sum→max）
        todo = [req for req in plan if req not in queried]
        if todo:
            outcomes: dict[str, tuple[str, Any]] = {}
            max_workers = min(len(todo), 4)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                future_map = {pool.submit(ag._query_one, req, task, context): req for req in todo}
                for fut in concurrent.futures.as_completed(future_map):
                    req = future_map[fut]
                    try:
                        outcomes[req] = ("ok", fut.result())
                    except Exception as exc:
                        outcomes[req] = ("fail", str(exc))
            # 按 plan 顺序收集，保持 observations/sql_history 顺序稳定
            for req in todo:
                status, payload = outcomes[req]
                if status == "ok":
                    obs = payload
                    observations.append(obs)
                    sql_history.append({"requirement": req, "sql": obs.get("sql"), "ok": True})
                else:
                    logger.warning("logistics_graph.query.fail", requirement=req, error=payload)
                    sql_history.append({"requirement": req, "ok": False, "error": payload})
                queried.add(req)

        return {
            "observations": observations,#观察结果
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
        analysis, evidence, enough, missing = ag._analyze(task, observations)
        final_result = ag._build_result(task, observations, list(state.get("sql_history") or []), analysis, evidence)
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

    builder = StateGraph(LogisticsState)
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


def run_logistics(task: str, context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """便捷入口：直接运行 Logistics SubGraph，返回 final_result。"""
    app = build_logistics_agent()
    result = app.invoke(initial_state(task, context))
    return result.get("final_result") or {"agent": "logistics", "error": "no result"}
