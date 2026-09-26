"""长期记忆提取器（设计文档 34-35 节）。

两级触发（避免每轮都调 LLM 提取）：
1. 规则预筛：user_question + final_answer 命中偏好/陈述关键词 → 触发
2. 历史兜底：会话历史 token > settings.MEMORY_EXTRACT_THRESHOLD → 触发
3. 会话关闭：API 显式调用 extract_memories_now()（绕过预筛，强制提取）

提取后写入决策：
- profiles     → confidence ≥ 0.8 且 evidence 非空才写 user_profiles
- preferences  → evidence 非空即可（latest-wins 覆盖成本低）
- memories     → 与已有记忆相似度去重（semantic.add_memory 内部处理）
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.config.settings import settings
from app.llm import get_chat_model, llm_available
from app.llm.structured import extract_json, invoke_structured, invoke_text
from app.memory.profile import upsert_preference, upsert_profile
from app.memory.semantic import add_memory
from app.observability.logging import get_logger

logger = get_logger("memory_extractor")

# 规则预筛关键词（命中即触发提取）
_TRIGGER_KEYWORDS = re.compile(
    r"我喜欢|我更关注|以后都用|以后看|默认|我是|我负责|我习惯|比较看重|我们一般|"
    r"通常|偏好|喜欢看|重点关注|记得|别忘了|用中文|用美元|美国市场|日本市场"
)

_PROFILE_CONFIDENCE_MIN = 0.8  # 画像写入阈值（evidence 非空为前提）

EXTRACT_PROMPT = """你是用户长期记忆提取器。基于以下一轮对话，提取"值得长期记住"的信息。

输入：
用户问题：{question}
系统回答：{answer}

提取规则：
1. 只提取跨会话仍然有用的信息（身份、职责、稳定偏好、业务规则、重要历史结论）；
2. 不提取临时指令（"今天查一下""这次看看"）、一次性上下文；
3. evidence 必须引用用户原话（从用户问题中摘录），摘不出来就不写该条；
4. confidence 评分标准：显式陈述（"我是/我负责/以后都用"）=0.9+；强推断=0.7-0.8；弱推断<0.6；
5. profiles.key 只从 [role, industry, market_scope, language] 中选；
   preferences.key 只从 [default_market, default_currency, time_range, report_format] 中选；
6. memories.type 只从 [preference, fact, conclusion, rule] 中选；department 为该条记忆最相关的部门
   （operation/finance/logistics/product），无法判断则留空（空=通用记忆）；
7. 没有值得记的信息就输出空列表，不要编造。

只输出 JSON，不要 markdown 代码块：
{{"profiles": [{{"key": "...", "value": "...", "confidence": 0.9, "evidence": "用户原话"}}],
  "preferences": [{{"key": "...", "value": "...", "evidence": "用户原话"}}],
  "memories": [{{"type": "fact", "content": "...", "department": "operation", "confidence": 0.9, "evidence": "依据"}}]}}
"""


def _parse_extract_json(raw: str) -> dict[str, Any]:
    """从 LLM 输出中提取 JSON（统一委托 extract_json，失败返回空 dict）。"""
    return extract_json(raw) or {}


# ---------------------------------------------------------------------------
# OPT-04 结构化输出：记忆提取 schema（with_structured_output 用）
# ---------------------------------------------------------------------------

class ExtractProfileItem(BaseModel):
    key: str = ""
    value: str = ""
    confidence: float = 0.0
    evidence: str = ""


class ExtractPreferenceItem(BaseModel):
    key: str = ""
    value: str = ""
    evidence: str = ""


class ExtractMemoryItem(BaseModel):
    type: str = "fact"
    content: str = ""
    department: str = ""
    confidence: float = 0.0
    evidence: str = ""


class ExtractOutput(BaseModel):
    """记忆提取输出 schema（与 EXTRACT_PROMPT 要求 JSON 同构）。"""
    profiles: list[ExtractProfileItem] = Field(default_factory=list)
    preferences: list[ExtractPreferenceItem] = Field(default_factory=list)
    memories: list[ExtractMemoryItem] = Field(default_factory=list)


def _rule_trigger(question: str, answer: str) -> bool:
    """规则预筛：命中偏好/陈述关键词即返回 True（零成本，不调 LLM）。"""
    return bool(_TRIGGER_KEYWORDS.search((question or "") + " " + (answer or "")[:2000]))


def _extract_once(user_id: str, question: str, answer: str) -> dict[str, Any]:
    """执行一次 LLM 提取记忆并写入（返回写入统计）。"""
    if not llm_available():
        return {"triggered": False, "reason": "llm_unavailable"}
    model = get_chat_model(tier="medium")
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content="你只做长期记忆提取，严格按用户要求输出 JSON。"),
        HumanMessage(content=EXTRACT_PROMPT.format(question=(question or "")[:3000], answer=(answer or "")[:4000])),
    ]
    # OPT-04：结构化通道优先，失败降级文本解析
    d = invoke_structured(model, ExtractOutput, messages, logger_name="memory_extractor")
    if d is not None:
        parsed = d
    else:
        text = invoke_text(model, messages, logger_name="memory_extractor")
        if text is None:
            logger.warning("memory.extract.failed", user_id=user_id)
            return {"triggered": True, "reason": "llm_failed", "profiles": 0, "preferences": 0, "memories": 0, "skipped": []}
        parsed = _parse_extract_json(text)
    stats = {"profiles": 0, "preferences": 0, "memories": 0, "skipped": []}

    # 1) 画像：高置信 + evidence 非空
    for p in parsed.get("profiles") or []:
        if not isinstance(p, dict):
            continue
        key = str(p.get("key", "")).strip()
        value = str(p.get("value", "")).strip()
        evidence = str(p.get("evidence", "")).strip()
        conf = float(p.get("confidence", 0) or 0)
        if not key or not value or not evidence:
            stats["skipped"].append(f"profile:{key or '?'}:no_evidence")
            continue
        if conf < _PROFILE_CONFIDENCE_MIN:
            stats["skipped"].append(f"profile:{key}:low_conf:{conf}")
            continue
        upsert_profile(user_id, key, value, confidence=conf, evidence=evidence)
        stats["profiles"] += 1

    # 2) 偏好：evidence 由提取规则保证（LLM 已过滤），latest-wins 覆盖
    for p in parsed.get("preferences") or []:
        if not isinstance(p, dict):
            continue
        key = str(p.get("key", "")).strip()
        value = str(p.get("value", "")).strip()
        evidence = str(p.get("evidence", "")).strip()
        if not key or not value:
            continue
        upsert_preference(user_id, key, value, evidence=evidence or None)
        stats["preferences"] += 1

    # 3) 非结构化：diff 去重由 semantic.add_memory 内部处理
    for m in parsed.get("memories") or []:
        if not isinstance(m, dict):
            continue
        mtype = str(m.get("type", "fact")).strip() or "fact"
        content = str(m.get("content", "")).strip()
        if not content:
            continue
        dept = str(m.get("department", "")).strip() or None
        evidence = str(m.get("evidence", "")).strip() or None
        try:
            conf = float(m.get("confidence", 0) or 0) or None
        except (TypeError, ValueError):
            conf = None
        add_memory(user_id, mtype, content, department=dept, evidence=evidence, confidence=conf)
        stats["memories"] += 1

    logger.info(
        "memory.extract.done",
        user_id=user_id,
        profiles=stats["profiles"],
        preferences=stats["preferences"],
        memories=stats["memories"],
        skipped=stats["skipped"][:5],
    )
    return {"triggered": True, **stats}


def maybe_extract_memories(
    user_id: str,
    thread_id: str,
    user_question: str,
    final_answer: str,
    history_tokens: int = 0,
) -> dict[str, Any]:
    """两/三级触发入口（run_question 结束钩子调用）。

    触发条件（任一）：
    - 规则预筛命中
    - 历史 token > settings.MEMORY_EXTRACT_THRESHOLD
    """
    triggered = False
    reason = ""
    if _rule_trigger(user_question, final_answer or ""):
        triggered, reason = True, "rule_hit"
    elif history_tokens >= settings.MEMORY_EXTRACT_THRESHOLD:
        triggered, reason = True, "history_threshold"
    if not triggered:
        return {"triggered": False, "reason": "no_signal"}
    logger.info("memory.extract.triggered", user_id=user_id, thread_id=thread_id, reason=reason)
    return _extract_once(user_id, user_question, final_answer)


def extract_memories_now(user_id: str, thread_id: str, user_question: str, final_answer: str) -> dict[str, Any]:
    """强制提取（会话关闭钩子 / 手动触发，绕过预筛）。"""
    logger.info("memory.extract.forced", user_id=user_id, thread_id=thread_id)
    return _extract_once(user_id, user_question, final_answer)
