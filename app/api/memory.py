"""长期记忆管理接口（设计文档 34-35 节）。

- GET    /memory?user_id=xx         查看用户全部记忆（画像/偏好/非结构化）
- DELETE /memory/{memory_id}        删除一条非结构化记忆（软删）
- POST   /memory/extract            强制触发记忆提取（会话关闭 / 手动触发）
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.memory.profile import get_preferences_detail, get_profiles_detail
from app.memory.semantic import delete_memory, list_memories
from app.observability.logging import get_logger

logger = get_logger("api_memory")

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryExtractRequest(BaseModel):
    """记忆提取请求体（会话关闭时触发）。"""
    thread_id: str = Field(..., description="会话 ID")
    user_id: Optional[str] = Field("default", description="用户 ID")
    user_question: str = Field("", description="最后一轮用户问题")
    final_answer: str = Field("", description="最后一轮系统回答")


@router.get("")
def list_user_memory(user_id: str = "default") -> dict[str, Any]:
    """查看用户全部记忆（结构化 + 非结构化）。"""
    try:
        profiles = get_profiles_detail(user_id)
        preferences = get_preferences_detail(user_id)
        memories = list_memories(user_id)
    except Exception as exc:
        logger.error("api.memory.list_failed", user_id=user_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"读取记忆失败: {exc}")
    logger.info("api.memory.list", user_id=user_id, profiles=len(profiles), preferences=len(preferences), memories=len(memories))
    return {
        "user_id": user_id,
        "profiles": profiles,
        "preferences": preferences,
        "memories": memories,
    }


@router.delete("/{memory_id}")
def remove_memory(memory_id: int) -> dict[str, Any]:
    """删除一条非结构化记忆（软删，superseded_at 标记）。"""
    ok = delete_memory(memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"记忆 {memory_id} 不存在或已删除")
    logger.info("api.memory.deleted", memory_id=memory_id)
    return {"deleted": True, "memory_id": memory_id}


@router.post("/extract")
def extract_memory(req: MemoryExtractRequest) -> dict[str, Any]:
    """强制触发记忆提取（会话关闭钩子；绕过规则预筛）。"""
    from app.memory.extractor import extract_memories_now

    try:
        stats = extract_memories_now(
            user_id=req.user_id,
            thread_id=req.thread_id,
            user_question=req.user_question,
            final_answer=req.final_answer,
        )
    except Exception as exc:
        logger.error("api.memory.extract_failed", thread_id=req.thread_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"记忆提取失败: {exc}")
    logger.info("api.memory.extracted", thread_id=req.thread_id, stats=stats)
    return {"thread_id": req.thread_id, **stats}
