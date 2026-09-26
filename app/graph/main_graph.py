"""主图：Parent Graph + Department SubGraph（设计文档 7 节 / 58 节最小闭环）。

执行流程（循环路由模式）：
  START -> manager -> router -> [department subgraph] -> router -> ... -> decision -> END

Phase 1 最小闭环：Manager -> Operation -> Decision（设计文档 58 节）。
后续 Phase 3 接入 finance/logistics/product 后，Router 自动调度（无需改主图结构）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from langchain_core.runnables import RunnableConfig

from app.agents.decision import DecisionAgent
from app.agents.manager import ManagerAgent
from app.config.settings import settings
from app.graph.quality import check_answer_quality, llm_quality_check
from app.graph.router import make_department_node, route_fn, router_node
from app.graph.state import GlobalState
from app.memory.injection import build_department_memory, build_manager_memory
from app.observability.logging import get_logger
from app.observability.tracing import get_run_url_by_id, init_langsmith

logger = get_logger("main_graph")


def build_main_graph(
    manager_agent: Optional[ManagerAgent] = None,
    decision_agent: Optional[DecisionAgent] = None,
    checkpointer: Any = None,
):
    """构建并编译主 Graph。

    Args:
        manager_agent: 可注入自定义 ManagerAgent（测试用）。
        decision_agent: 可注入自定义 DecisionAgent（测试用）。
        checkpointer: LangGraph checkpointer（设计文档 33 节，PostgresSaver）；
            传入后编译图支持中断恢复 / 故障重试 / time travel。

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
        user_id = state.get("user_id", "default")
        logger.info("main.manager.start", question=user_question[:100])
        try:
            # 长期记忆注入（用户级：画像 + 偏好 + 通用非结构化 top-k，设计文档 34-35 节）
            memory = build_manager_memory(user_id, user_question)
            task_plan = mgr.run(
                user_question,
                memory=json.dumps(memory, ensure_ascii=False) if memory else None,
            )
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

    # 部门记忆注入（设计文档 34-35 节：分层注入——部门执行时给部门上下文）
    def _dept_memory_builder(agent_name: str):
        def builder(state: dict[str, Any]) -> Optional[dict[str, Any]]:
            return build_department_memory(
                state.get("user_id", "default"),
                agent_name,
                state.get("user_question", ""),
            ) or None
        return builder

    # 部门节点（operation / finance / logistics / product 均已实现）
    operation_node = make_department_node("operation", context_builder=_dept_memory_builder("operation"))
    finance_node = make_department_node("finance", context_builder=_dept_memory_builder("finance"))
    logistics_node = make_department_node("logistics", context_builder=_dept_memory_builder("logistics"))

    # Product 节点：注入跨部门上下文（Operation/Finance/Logistics 结论摘要，设计文档 6 节）
    def _product_context_builder(state: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Product 上下文 = 跨部门结论（O/F/L）+ 部门记忆/业务规则（分层注入）。"""
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
        mem = build_department_memory(
            state.get("user_id", "default"), "product", state.get("user_question", "")
        )
        if mem:
            ctx.update(mem)
        return ctx or None

    product_node = make_department_node("product", context_builder=_product_context_builder)

    def _decision(state: dict[str, Any]) -> dict[str, Any]:
        """Decision 节点：汇总各部门结果 -> 结构化最终报告。

        支持质量自纠（考点二十九）：quality_gate 判不合格后带 feedback 回炉，
        本节点把 feedback 注入 DecisionAgent 重生成（部门结果保留在 state，不重跑 Router）。
        """
        user_question = state.get("user_question", "")
        user_id = state.get("user_id", "default")
        department_results = state.get("department_results") or {}
        feedback = state.get("quality_feedback") or ""
        skipped = state.get("skipped_tasks") or []
        if skipped:
            logger.info("main.decision.with_skipped", skipped=skipped)

        logger.info("main.decision.start", departments=list(department_results.keys()), has_feedback=bool(feedback))
        try:
            # 决策阶段注入用户级记忆（画像/偏好/通用记忆）：
            # Manager 只在规划时看到记忆，最终回答由 Decision 生成，
            # 若用户问"我负责哪个市场"这类自身相关问题，Decision 需能读到画像。
            memory = build_manager_memory(user_id, user_question)
            report = dec.run(
                user_question,
                department_results,
                memory=json.dumps(memory, ensure_ascii=False) if memory else None,
                feedback=feedback or None,
            )
            logger.info("main.decision.done", confidence=report.get("confidence"))
            return {
                "decision_result": report,
                "final_answer": report.get("summary", ""),
                # feedback 已消费，清空避免下一轮残留
                "quality_feedback": "",
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
    # quality_gate：答案质量自纠回路（考点二十九）
    # ------------------------------------------------------------------

    def _quality_gate(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        """质量门：评估 Decision 输出，决定 END / 回炉 / interrupt 等用户。

        流程（条件边 _quality_route 承接）：
        1. 规则检查（确定性零成本）+ 可选 LLM 裁判（settings.QUALITY_GATE_JUDGE_ENABLED）；
        2. 合格 -> END；
        3. 不合格且未达自动回炉上限 -> 带 quality_feedback 回 decision 重生成；
        4. 自动回炉耗尽 -> human_in_the_loop=True 时 interrupt() 暂停等用户纠正（approve/revise），
           否则放行（force_pass，保留诊断供日志/前端查看）。

        interrupt() 需要编译时传 checkpointer；无 checkpointer 或调用失败时降级放行。
        """
        user_question = state.get("user_question", "")
        decision_result = state.get("decision_result") or {}
        summary = str(decision_result.get("summary") or state.get("final_answer") or "").strip()

        # 1) 规则检查（永远启用）
        verdict = check_answer_quality(user_question, decision_result)
        issues = list(verdict["issues"])

        # 2) 可选 LLM 裁判：规则通过后再查跑题/漏答（默认关闭，开启后每次回答多一次 small 调用）
        if not issues and settings.QUALITY_GATE_JUDGE_ENABLED:
            issues = llm_quality_check(user_question, summary or "(空)")

        iteration = int(state.get("quality_iteration") or 0)
        if not issues:
            logger.info("quality_gate.pass", iteration=iteration)
            return {
                "quality_check": {"pass": True, "issues": []},
                "quality_feedback": "",
                "current_stage": "done",
            }

        # ---- 不合格 ----
        # interrupt() 通过抛出 GraphInterrupt 实现暂停（执行器捕获后返回 __interrupt__），
        # 因此不能用 try/except 包裹；无 checkpointer 编译时由 _hilt_ok 守卫降级（不 interrupt）。
        human_in_the_loop = bool(
            (config or {}).get("configurable", {}).get("human_in_the_loop", False)
        )
        _hilt_ok = human_in_the_loop and checkpointer is not None
        if iteration >= settings.QUALITY_GATE_MAX_AUTO_RETRIES:
            if _hilt_ok:
                # 暂停执行，把候选答案和偏差诊断交给用户；resume 时 interrupt() 返回用户输入
                # ** `interrupt()` = "暂停点 + 断点落盘 + 控制权交还"，等待发生在你的应用层，回来靠checkpoint续跑 **—— 不是"挂起等输入"
                feedback = interrupt({
                    "type": "quality_feedback",
                    "question": user_question,
                    "draft_answer": summary,
                    "issues": issues,
                    "iteration": iteration,
                })

                if isinstance(feedback, dict) and feedback.get("action") == "approve":
                    logger.info("quality_gate.user.approve", iteration=iteration)
                    return {
                        "quality_check": {"pass": True, "approved": True, "issues": issues},
                        "quality_feedback": "",
                        "current_stage": "done",
                    }
                fb = ""
                if isinstance(feedback, dict):
                    fb = str(feedback.get("feedback") or "").strip()
                elif feedback is not None:
                    fb = str(feedback).strip()
                if fb:
                    logger.info("quality_gate.user.revise", iteration=iteration, feedback=fb[:100])
                    return {
                        "quality_feedback": fb,
                        "quality_iteration": iteration + 1,
                        "quality_check": {"pass": False, "issues": issues, "source": "user"},
                        "current_stage": "revising",
                    }
                # 用户未给有效 feedback -> 放行（防死循环）
                logger.info("quality_gate.user.no_feedback_force_pass", iteration=iteration)
                return {
                    "quality_check": {"pass": True, "force_pass": True, "issues": issues},
                    "quality_feedback": "",
                    "current_stage": "done",
                }

            # 非交互或未编译 checkpointer：自动重试耗尽后放行，保留诊断供日志/前端查看
            logger.warning(
                "quality_gate.force_pass",
                iteration=iteration,
                hilt=human_in_the_loop,
                has_checkpointer=checkpointer is not None,
                issues=issues[:3],
            )
            return {
                "quality_check": {"pass": True, "force_pass": True, "issues": issues},
                "quality_feedback": "",
                "current_stage": "done",
            }

        # ---- 还有自动回炉额度：带 feedback 回 decision 重生成 ----
        fb = "；".join(issues)
        logger.info("quality_gate.revise.auto", iteration=iteration, issues=issues[:3])
        return {
            "quality_feedback": fb,
            "quality_iteration": iteration + 1,
            "quality_check": {"pass": False, "issues": issues, "source": "auto"},
            "current_stage": "revising",
        }

    def _quality_route(state: dict[str, Any]) -> str:
        """quality_gate 条件边：pass -> END；fail -> 回 decision 重生成。"""
        qc = state.get("quality_check") or {}
        if qc.get("pass"):
            return "end"
        return "revise"

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
    if settings.QUALITY_GATE_ENABLED:
        # 质量自纠回路（考点二十九）：decision -> quality_gate -> (END | 回 decision)
        builder.add_node("quality_gate", _quality_gate)
        builder.add_edge("decision", "quality_gate")
        builder.add_conditional_edges(
            "quality_gate",
            _quality_route,
            {"revise": "decision", "end": END},
        )
    else:
        # 开关关闭时保持旧行为：decision 直接到 END
        builder.add_edge("decision", END)

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

    return builder.compile(checkpointer=checkpointer)


def run_question(
    question: str,
    thread_id: str = "default",
    user_id: str = "default",
    checkpointer: Any = None,
    callbacks: Optional[list[Any]] = None,
    human_in_the_loop: bool = False,
) -> dict[str, Any]:
    """执行一次用户问题（主入口）。

    Args:
        question: 用户自然语言问题。
        thread_id: 会话 ID（checkpoint / 记忆的 key）。
        user_id: 用户 ID。
        checkpointer: 可注入 checkpointer（默认自动获取 PostgresSaver 单例）。
        callbacks: 可选 LangChain 回调（如评估脚本的 token 用量采集器），
            经 invoke config 透传给图内所有 LLM 调用。
        human_in_the_loop: 质量门自动回炉耗尽后是否 interrupt() 暂停等用户纠正
            （考点二十九；默认 False 即非交互放行，True 时调用方需处理 __interrupt__ 并 resume）。

    Returns:
        包含 final_answer / decision_result / department_results 的完整状态字典；
        human_in_the_loop 触发暂停时，结果含 __interrupt__ 键（见 quality_gate）。
    """
    logger.info("run_question.start", thread_id=thread_id, question=question[:100], hilt=human_in_the_loop)
    # OPT-02 LangSmith：任何 LLM/图调用前同步环境变量并挂载自动 tracer（幂等，失败仅降级）
    init_langsmith()
    if checkpointer is None:
        from app.memory.checkpoint import get_checkpointer
        checkpointer = get_checkpointer()
    app = build_main_graph(checkpointer=checkpointer)
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
    # 预生成顶层 run id：LangGraph/LangChain tracer 会用这个 id 作为本次提问的
    # 根 run（config 透传 run_id），返回结果里即可携带精确的 trace 链接（OPT-02）。
    run_id = uuid.uuid4()
    invoke_config: dict[str, Any] = {
        "run_id": run_id,
        "configurable": {
            "thread_id": thread_id,
            "human_in_the_loop": human_in_the_loop,
        }
    }
    if callbacks:
        invoke_config["callbacks"] = callbacks
    result = app.invoke(
        initial_state,
        config=invoke_config,
    )
    # trace 链接回业务：失败/未启用返回空串，不影响主链路
    trace_url = get_run_url_by_id(str(run_id))
    result["trace_url"] = trace_url
    logger.info(
        "run_question.done",
        thread_id=thread_id,
        stage=result.get("current_stage"),
        confidence=result.get("decision_result", {}).get("confidence"),
        run_id=str(run_id),
        trace_url=trace_url,
    )

    # 长期记忆提取钩子（两级触发：规则命中 / 历史阈值；写入失败不影响主流程）
    try:
        from app.memory.extractor import maybe_extract_memories

        answer = " ".join(filter(None, [
            result.get("final_answer", ""),
            str((result.get("decision_result") or {}).get("summary", "")),
        ]))
        maybe_extract_memories(
            user_id=user_id,
            thread_id=thread_id,
            user_question=question,
            final_answer=answer,
        )
    except Exception as exc:
        logger.warning("run_question.memory_extract.fail", error=str(exc))

    return result
