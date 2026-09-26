"""对话接口（设计文档 49 节 Chat）。

POST /chat -> 调用主 Graph（Manager -> 部门 Agent -> Decision -> quality_gate）-> 返回结构化结果。
POST /chat/{thread_id}/resume -> 质量门 interrupt() 暂停后，提交用户纠正意见/确认继续
（human-in-the-loop，考点二十九）。

Phase 1：同步返回完整结果。后续 Phase 9 可扩展为 SSE 流式推送 Agent 执行进度。
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.graph.main_graph import run_question
from app.observability.logging import get_logger

logger = get_logger("api_chat")

router = APIRouter(prefix="/chat", tags=["chat"])


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    """聊天请求体。"""
    question: str = Field(..., min_length=1, description="用户自然语言问题")
    thread_id: Optional[str] = Field(None, description="会话 ID（不传则自动生成）")
    user_id: Optional[str] = Field("default", description="用户 ID")
    human_in_the_loop: bool = Field(
        False,
        description="质量门自动回炉耗尽后是否暂停等用户纠正（考点二十九）；"
                    "True 时若答案仍不合格，接口返回 stage=awaiting_feedback，"
                    "需再调 /chat/{thread_id}/resume 提交意见或确认",
    )


class ResumeRequest(BaseModel):
    """human-in-the-loop 恢复请求体。"""
    action: str = Field(
        "revise",
        description='"approve"=接受当前答案放行；"revise"=带 feedback 重新生成',
    )
    feedback: Optional[str] = Field(None, description='action="revise" 时必填：用户纠正意见')


class ChatResponse(BaseModel):
    """聊天响应体。"""
    thread_id: str
    user_id: str
    question: str
    stage: str
    final_answer: str
    decision_result: dict[str, Any]
    department_results: dict[str, Any]
    required_agents: list[str]
    completed_tasks: list[str]
    skipped_tasks: list[str]
    quality_check: Optional[dict[str, Any]] = None
    quality_pending: Optional[dict[str, Any]] = Field(
        None, description='stage="awaiting_feedback" 时非空：待用户确认的候选答案与偏差诊断'
    )
    trace_url: Optional[str] = Field(
        None, description="本次执行的 LangSmith trace 链接（未启用 tracing 时为空）"
    )


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _extract_interrupt_payload(result: dict[str, Any]) -> Optional[dict[str, Any]]:
    """从 invoke 结果中提取首个 interrupt 的 payload（兼容 Interrupt 对象 / 原始值）。"""
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    it = interrupts[0]
    value = getattr(it, "value", it)
    return value if isinstance(value, dict) else {"payload": value}


def _build_response(
    thread_id: str,
    user_id: str,
    question: str,
    result: dict[str, Any],
) -> ChatResponse:
    """统一构造响应：正常结果 / 质量门暂停（awaiting_feedback）。"""
    pending = _extract_interrupt_payload(result)
    if pending is not None:
        logger.info("api.chat.awaiting_feedback", thread_id=thread_id, issues=pending.get("issues"))
        return ChatResponse(
            thread_id=thread_id,
            user_id=user_id,
            question=question,
            stage="awaiting_feedback",
            final_answer=result.get("final_answer", ""),
            decision_result=result.get("decision_result", {}),
            department_results=result.get("department_results", {}),
            required_agents=result.get("required_agents", []),
            completed_tasks=result.get("completed_tasks", []),
            skipped_tasks=result.get("skipped_tasks", []),
            quality_check=result.get("quality_check"),
            quality_pending=pending,
            trace_url=result.get("trace_url") or None,
        )

    return ChatResponse(
        thread_id=thread_id,
        user_id=user_id,
        question=question,
        stage=result.get("current_stage", "unknown"),
        final_answer=result.get("final_answer", ""),
        decision_result=result.get("decision_result", {}),
        department_results=result.get("department_results", {}),
        required_agents=result.get("required_agents", []),
        completed_tasks=result.get("completed_tasks", []),
        skipped_tasks=result.get("skipped_tasks", []),
        quality_check=result.get("quality_check"),
        quality_pending=None,
        trace_url=result.get("trace_url") or None,
    )


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@router.post("", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """提交一个问题，执行完整 Multi-Agent 流程并返回结构化结果。

    执行流程：Manager 规划 -> 部门 Agent 分析 -> Decision 汇总 -> quality_gate 质量门
    （不合格自动回炉；human_in_the_loop=True 且回炉耗尽时暂停等用户）。
    """
    thread_id = req.thread_id or str(uuid.uuid4())
    user_id = req.user_id or "default"

    logger.info(
        "api.chat.received",
        thread_id=thread_id,
        user_id=user_id,
        question=req.question[:100],
        human_in_the_loop=req.human_in_the_loop,
    )

    try:
        result = run_question(
            req.question,
            thread_id=thread_id,
            user_id=user_id,
            human_in_the_loop=req.human_in_the_loop,
        )
    except Exception as exc:
        logger.error("api.chat.execution_failed", thread_id=thread_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Agent 执行失败: {exc}")

    stage = result.get("current_stage", "unknown")
    if stage == "error":
        err = result.get("error_state", {})
        logger.error("api.chat.stage_error", thread_id=thread_id, error=err)
        raise HTTPException(status_code=500, detail=f"Agent 执行出错: {err.get('error', 'unknown')}")

    response = _build_response(thread_id, user_id, req.question, result)
    logger.info(
        "api.chat.done",
        thread_id=thread_id,
        stage=response.stage,
        confidence=response.decision_result.get("confidence"),
    )
    return response


@router.post("/{thread_id}/resume", response_model=ChatResponse)
def resume_chat(thread_id: str, body: ResumeRequest) -> ChatResponse:
    """quality_gate interrupt() 暂停后恢复执行（human-in-the-loop）。

    action="approve"：接受当前候选答案，放行到 END；
    action="revise"：把 feedback 注入 Decision 重新生成（最多再走一轮质量门）。
    依赖 checkpointer 从断点继续（time-travel 语义，考点十九）。
    """
    from app.graph.main_graph import build_main_graph
    from app.memory.checkpoint import get_checkpointer

    logger.info(
        "api.chat.resume.received",
        thread_id=thread_id,
        action=body.action,
        has_feedback=bool(body.feedback),
    )

    if body.action == "revise" and not (body.feedback or "").strip():
        raise HTTPException(status_code=422, detail='action="revise" 时必须提供非空 feedback')

    payload: dict[str, Any] = {"action": body.action}
    if body.action == "revise":
        payload["feedback"] = (body.feedback or "").strip()

    try:
        app = build_main_graph(checkpointer=get_checkpointer())
        result = app.invoke(
            Command(resume=payload),
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "human_in_the_loop": True,  # 继续支持再次 interrupt
                }
            },
        )
    except Exception as exc:
        logger.error("api.chat.resume.failed", thread_id=thread_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"恢复执行失败: {exc}")

    # 从恢复的 state 里取原始问题/用户（checkpoint 已保存）
    response = _build_response(
        thread_id,
        result.get("user_id", "default"),
        result.get("user_question", ""),
        result,
    )
    logger.info(
        "api.chat.resume.done",
        thread_id=thread_id,
        stage=response.stage,
        quality_check=response.quality_check,
    )
    return response
