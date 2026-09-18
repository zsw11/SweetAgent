"""对话接口（设计文档 49 节 Chat）。

POST /chat -> 调用主 Graph（Manager -> 部门 Agent -> Decision）-> 返回结构化结果。

Phase 1：同步返回完整结果。后续 Phase 9 可扩展为 SSE 流式推送 Agent 执行进度。
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
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


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@router.post("", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """提交一个问题，执行完整 Multi-Agent 流程并返回结构化结果。

    执行流程：Manager 规划 -> 部门 Agent 分析 -> Decision 汇总决策。
    """
    thread_id = req.thread_id or str(uuid.uuid4())
    user_id = req.user_id or "default"

    logger.info(
        "api.chat.received",
        thread_id=thread_id,
        user_id=user_id,
        question=req.question[:100],
    )

    try:
        result = run_question(req.question, thread_id=thread_id, user_id=user_id)
    except Exception as exc:
        logger.error("api.chat.execution_failed", thread_id=thread_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Agent 执行失败: {exc}")

    stage = result.get("current_stage", "unknown")
    if stage == "error":
        err = result.get("error_state", {})
        logger.error("api.chat.stage_error", thread_id=thread_id, error=err)
        raise HTTPException(status_code=500, detail=f"Agent 执行出错: {err.get('error', 'unknown')}")

    response = ChatResponse(
        thread_id=thread_id,
        user_id=user_id,
        question=req.question,
        stage=stage,
        final_answer=result.get("final_answer", ""),
        decision_result=result.get("decision_result", {}),
        department_results=result.get("department_results", {}),
        required_agents=result.get("required_agents", []),
        completed_tasks=result.get("completed_tasks", []),
        skipped_tasks=result.get("skipped_tasks", []),
    )
    logger.info(
        "api.chat.done",
        thread_id=thread_id,
        stage=stage,
        confidence=response.decision_result.get("confidence"),
    )
    return response
