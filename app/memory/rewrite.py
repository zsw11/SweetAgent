"""Query 改写（OPT-12：多轮指代消解）。

run_question 入口、进图前调用：当会话已有历史时，让 small 模型结合会话上下文把
本轮问题改写为"不依赖上下文也能理解"的自包含问题，补全指代词（它/这个/那/该）、
省略的实体与时间范围：
    "那物流呢" → "美国床垫的物流时效与退货率对比"

首轮无历史、或 LLM 不可用时跳过（返回原文，零开销）。原文保留在 state 供记忆
提取/审计，改写版仅作为 Manager/Decision 的上下文。
"""

from __future__ import annotations

import re

from app.llm import get_chat_model, llm_available
from app.llm.structured import invoke_text
from app.observability.logging import get_logger

logger = get_logger("query_rewrite")

REWRITE_SYSTEM = "你是查询改写器，只输出改写后的问题本身，不做解释。"

REWRITE_PROMPT = """下面是用户与商业分析助手的多轮会话上下文，以及用户本轮提出的新问题。
请把本轮问题改写为"脱离上下文也能完整理解"的自包含问题：补全其中的指代词（它、这个、那个、该、其）、
省略的实体（市场/品牌/品类/部门）与时间范围，使其可独立用于分析。

【会话上下文】
{context}

【本轮问题】
{question}

改写要求：
1. 只输出改写后的问题本身，不要任何解释、前缀或引号；
2. 若本轮问题本身已自包含、无需改写，则原样输出；
3. 不得改变用户意图，也不得添加会话中没有出现的信息。
"""

# 轻量清洗：去除模型可能误加的前缀 / 包裹引号
_PREFIX_RE = re.compile(r"^\s*(改写后(的问题)?|问题|rewritten)\s*[:：]\s*", re.IGNORECASE)


def _clean(text: str) -> str:
    text = (text or "").strip()
    text = _PREFIX_RE.sub("", text).strip()
    # 去首尾成对引号
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”‘’":
        text = text[1:-1].strip()
    return text


def rewrite_question(user_question: str, conversation_context: str) -> str:
    """结合会话上下文改写本轮问题；无条件改写时返回原文。

    Args:
        user_question: 用户本轮原始问题。
        conversation_context: app.memory.conversation.build_conversation_context 产物，
            空串表示首轮/无历史 → 直接返回原文。

    Returns:
        改写后的自包含问题；改写失败/无需改写时返回原文。
    """
    if not (user_question or "").strip() or not (conversation_context or "").strip():
        return user_question
    if not llm_available():
        logger.info("query.rewrite.skip_no_llm")
        return user_question

    from langchain_core.messages import HumanMessage, SystemMessage

    model = get_chat_model(tier="small")
    messages = [
        SystemMessage(content=REWRITE_SYSTEM),
        HumanMessage(
            content=REWRITE_PROMPT.format(
                context=conversation_context[:6000],
                question=user_question[:2000],
            )
        ),
    ]
    raw = invoke_text(model, messages, logger_name="query_rewrite")
    rewritten = _clean(raw or "")
    if not rewritten:
        logger.info("query.rewrite.empty_use_original")
        return user_question

    if rewritten.strip() != user_question.strip():
        logger.info(
            "query.rewritten",
            original=user_question[:100],
            rewritten=rewritten[:150],
        )
    return rewritten
