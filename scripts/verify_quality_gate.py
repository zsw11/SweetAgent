# -*- coding: utf-8 -*-
"""质量自纠回路（考点二十九）集成验证。

用 InMemorySaver + mock Manager/Decision Agent 隔离验证，不依赖真实 LLM / Postgres：
  A. 自动回炉：劣质答案 -> quality_gate 不合格 -> 带 feedback 回 decision -> 修正通过
  B. human-in-the-loop：自动回炉耗尽 -> interrupt() 暂停 -> resume approve -> 放行 END
  C. human-in-the-loop：resume revise 带用户 feedback -> decision 收到并修正
  D. 非交互放行：hilt=False，回炉耗尽后 force_pass（不 interrupt，不阻塞）

运行：.venv\\Scripts\\python.exe scripts\\verify_quality_gate.py
"""

import sys

sys.path.insert(0, ".")

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

import app.graph.main_graph as _mg  # noqa: E402

# 隔离：记忆注入连真实 Postgres（向量检索），mock 掉避免测试依赖数据库
_mg.build_manager_memory = lambda user_id, question: None
_mg.build_department_memory = lambda user_id, agent_name, question: None

from app.config.settings import settings  # noqa: E402
from app.graph.main_graph import build_main_graph, run_question  # noqa: E402

BAD_SUMMARY = ""  # 空结论 -> 规则"过短"必命中
GOOD_SUMMARY = "美国市场六月销售额环比下降 8.2%，主因头部 SKU 缺货，建议 P0 补货并核查物流时效。"


class MockManager:
    """规划器 mock：不派任何部门任务，直接到 decision（隔离质量门逻辑）。"""

    def run(self, question, memory=None):
        return {"intent": "test", "required_agents": [], "tasks": []}


class MockDecision:
    """Decision mock：按预置响应序列返回，并记录每次收到的 feedback。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []  # 每次调用的 feedback 记录

    def run(self, user_question, department_results, memory=None, feedback=None, injection_warning=""):
        self.calls.append(feedback)
        summary = self.responses.pop(0)
        return {
            "summary": summary,
            "findings": [],
            "root_causes": [],
            "recommendations": [],
            "risks": [],
            "confidence": 0.8 if summary else 0.0,
        }


def _initial_state(thread_id):
    return {
        "thread_id": thread_id,
        "user_id": "test",
        "user_question": "美国市场六月为什么销量下滑？",
        "current_stage": "planning",
        "department_results": {},
        "completed_tasks": [],
        "skipped_tasks": [],
        "current_task": "",
    }


def _config(thread_id, hilt):
    return {"configurable": {"thread_id": thread_id, "human_in_the_loop": hilt}}


def scenario_a_auto_revise():
    """A：自动回炉一轮后修正成功。"""
    dec = MockDecision([BAD_SUMMARY, GOOD_SUMMARY])
    app = build_main_graph(manager_agent=MockManager(), decision_agent=dec, checkpointer=InMemorySaver())
    result = app.invoke(_initial_state("t-a"), config=_config("t-a", hilt=False))

    assert result.get("current_stage") == "done", f"A: stage={result.get('current_stage')}"
    assert result.get("quality_check", {}).get("pass") is True, f"A: {result.get('quality_check')}"
    assert result.get("quality_iteration") == 1, f"A: iteration={result.get('quality_iteration')}"
    assert len(dec.calls) == 2, f"A: decision 应调用 2 次，实际 {len(dec.calls)}"
    assert dec.calls[1], f"A: 第 2 次应带 feedback，实际 {dec.calls[1]!r}"
    assert result.get("final_answer") == GOOD_SUMMARY
    print("[A] 自动回炉修正成功：劣质->feedback->重生成->通过  ✅")


def scenario_b_hilt_approve():
    """B：回炉耗尽 -> interrupt 暂停 -> resume approve -> END。"""
    dec = MockDecision([BAD_SUMMARY, BAD_SUMMARY, BAD_SUMMARY])
    app = build_main_graph(manager_agent=MockManager(), decision_agent=dec, checkpointer=InMemorySaver())
    result = app.invoke(_initial_state("t-b"), config=_config("t-b", hilt=True))

    interrupts = result.get("__interrupt__")
    assert interrupts, "B: 应触发 interrupt"
    payload = getattr(interrupts[0], "value", interrupts[0])
    assert payload.get("type") == "quality_feedback", f"B: payload={payload}"
    assert payload.get("issues"), "B: payload 应带 issues"
    assert payload.get("draft_answer") == BAD_SUMMARY

    resumed = app.invoke(Command(resume={"action": "approve"}), config=_config("t-b", hilt=True))
    assert resumed.get("current_stage") == "done", f"B: resume stage={resumed.get('current_stage')}"
    assert resumed.get("quality_check", {}).get("approved") is True
    assert not resumed.get("__interrupt__"), "B: approve 后不应再 interrupt"
    print("[B] human-in-the-loop：interrupt 暂停 -> approve 放行 -> END  ✅")


def scenario_c_hilt_revise():
    """C：resume revise 带用户 feedback -> decision 收到并修正通过。"""
    dec = MockDecision([BAD_SUMMARY, BAD_SUMMARY, BAD_SUMMARY, GOOD_SUMMARY])
    app = build_main_graph(manager_agent=MockManager(), decision_agent=dec, checkpointer=InMemorySaver())
    result = app.invoke(_initial_state("t-c"), config=_config("t-c", hilt=True))
    assert result.get("__interrupt__"), "C: 应先 interrupt"

    resumed = app.invoke(
        Command(resume={"action": "revise", "feedback": "请正面回答销量下滑的具体原因和补救建议"}),
        config=_config("t-c", hilt=True),
    )
    assert resumed.get("current_stage") == "done", f"C: resume stage={resumed.get('current_stage')}"
    assert resumed.get("final_answer") == GOOD_SUMMARY
    assert dec.calls[3] == "请正面回答销量下滑的具体原因和补救建议", f"C: feedback 未传给 decision: {dec.calls}"
    assert resumed.get("quality_check", {}).get("pass") is True
    print("[C] human-in-the-loop：resume revise 携带用户 feedback -> decision 收到并修正  ✅")


def scenario_d_force_pass():
    """D：非交互（默认）回炉耗尽后 force_pass，不 interrupt 不阻塞。"""
    dec = MockDecision([BAD_SUMMARY, BAD_SUMMARY, BAD_SUMMARY])
    app = build_main_graph(manager_agent=MockManager(), decision_agent=dec, checkpointer=InMemorySaver())
    result = app.invoke(_initial_state("t-d"), config=_config("t-d", hilt=False))

    assert not result.get("__interrupt__"), "D: 非交互不应 interrupt"
    assert result.get("current_stage") == "done"
    assert result.get("quality_check", {}).get("force_pass") is True
    assert result.get("quality_iteration") == settings.QUALITY_GATE_MAX_AUTO_RETRIES
    assert len(dec.calls) == settings.QUALITY_GATE_MAX_AUTO_RETRIES + 1
    print("[D] 非交互：回炉耗尽 force_pass（不阻塞调用方）✅")


def scenario_e_run_question_compat():
    """E：run_question 入口兼容（真实链路冒烟，LLM 可用时手动跑 verify_pipeline 验证）。"""
    # 真实入口走完整主图（依赖 LLM + Postgres），由 scripts/verify_pipeline.py 手动冒烟；
    # 此处仅确认 run_question 的签名与默认参数（hilt=False）不破坏导入。
    import inspect

    sig = inspect.signature(run_question)
    assert "human_in_the_loop" in sig.parameters
    assert sig.parameters["human_in_the_loop"].default is False
    print("[E] run_question 新增 human_in_the_loop 参数（默认 False，向后兼容）✅")


if __name__ == "__main__":
    scenario_a_auto_revise()
    scenario_b_hilt_approve()
    scenario_c_hilt_revise()
    scenario_d_force_pass()
    scenario_e_run_question_compat()
    print("\n全部质量自纠验证通过 ✅")
