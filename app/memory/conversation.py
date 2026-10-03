"""多轮会话历史存储（OPT-12：上下文注入 / query 改写 / token 统计 / 压缩）。

按 thread_id 在 conversation_messages 表存"用户问 + 系统答"，区别于 checkpoints
（图执行快照，仅供中断恢复 / time travel，不面向应用查询）。

提供：
- count_tokens：消息 token 计数（tiktoken，离线降级启发式）
- append_message：追加一条消息（自动 token 计数 + 会话内自增 seq）
- get_messages：按 seq 取会话消息（含 summary 压缩摘要）
- history_token_count：会话当前总 token（压缩/提取阈值统计）
- get_summary / get_recent_turns：取压缩摘要 / 最近 N 轮原文
- build_conversation_context：构造注入 Manager/Decision 的会话上下文文本
  （历史摘要 + 最近 N 轮原文），无历史返回空串

连接：settings.DATABASE_URL（app_user，与长期记忆表同库），psycopg 短连接，
FastAPI 多线程安全。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from app.config.settings import settings
from app.llm import get_chat_model, llm_available
from app.llm.structured import invoke_text
from app.memory.db import connect  # 与长期记忆表同库同连接方式（复用，不重复定义）
from app.observability.logging import get_logger

logger = get_logger("conversation")


# ---------------------------------------------------------------------------
# token 计数（tiktoken 优先；离线/无缓存时降级为中英混合启发式）
# ---------------------------------------------------------------------------

_ENCODER = None
_ENCODER_TRIED = False


def _get_encoder():
    """惰性加载 tiktoken cl100k_base（失败返回 None，进程内只尝试一次）。"""
    global _ENCODER, _ENCODER_TRIED
    if not _ENCODER_TRIED:
        _ENCODER_TRIED = True
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception as exc:  # 离线缺 blob 等
            logger.warning("conversation.token_encoder.fallback", error=str(exc))
            _ENCODER = None
    return _ENCODER


def _heuristic_tokens(text: str) -> int:
    """无 tiktoken 时的近似计数：CJK 1 字≈1 token，其余约 4 字符≈1 token。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3000-\u30ff]", text or ""))
    other = max(0, len(text or "") - cjk)
    return cjk + (other + 3) // 4


def count_tokens(text: str) -> int:
    """统计文本 token 数（空文本 0）。阈值统计为近似值，目的是触发而非计费。"""
    if not text:
        return 0
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return _heuristic_tokens(text)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def append_message(thread_id: str, role: str, content: str, user_id: str = "default") -> dict[str, Any]:
    """追加一条会话消息，返回 {seq, tokens}。

    seq 取会话当前最大 seq + 1（同事务内计算，并发追加靠 (thread_id,seq) 唯一约束兜底）；
    role: user / assistant / summary。
    """
    content = content or ""
    tokens = count_tokens(content)
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM conversation_messages WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
        seq = (row[0] if row else -1) + 1
        conn.execute(
            "INSERT INTO conversation_messages (thread_id, user_id, role, content, tokens, seq) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (thread_id, user_id, role, content, tokens, seq),
        )
        conn.commit()
    return {"seq": seq, "tokens": tokens}


def append_turn(
    thread_id: str,
    user_question: str,
    answer: str,
    user_id: str = "default",
) -> dict[str, Any]:
    """一个事务追加一轮（user + assistant），返回 {user_seq, assistant_seq, history_tokens}。

    相比两次 append_message：1 次连接 + 1 个事务（seq 连续分配），并直接返回
    追加后的历史总 token（供提取/压缩阈值判断，省一次 SUM 查询）。
    并发冲突由 (thread_id, seq) 唯一约束兜底（单用户场景风险极低）。
    """
    user_question = user_question or ""
    answer = answer or ""
    user_tokens = count_tokens(user_question)
    answer_tokens = count_tokens(answer)
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM conversation_messages WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
        base = (row[0] if row else -1) + 1
        conn.execute(
            "INSERT INTO conversation_messages (thread_id, user_id, role, content, tokens, seq) "
            "VALUES (%s, %s, 'user', %s, %s, %s)",
            (thread_id, user_id, user_question, user_tokens, base),
        )
        conn.execute(
            "INSERT INTO conversation_messages (thread_id, user_id, role, content, tokens, seq) "
            "VALUES (%s, %s, 'assistant', %s, %s, %s)",
            (thread_id, user_id, answer, answer_tokens, base + 1),
        )
        total = conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM conversation_messages WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()[0]
        conn.commit()
    return {"user_seq": base, "assistant_seq": base + 1, "history_tokens": int(total)}


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def get_messages(thread_id: str) -> list[dict[str, Any]]:
    """按 seq 升序返回会话全部消息（含 summary）。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content, tokens, seq FROM conversation_messages "
            "WHERE thread_id = %s ORDER BY seq ASC",
            (thread_id,),
        ).fetchall()
    return [
        {"role": r[0], "content": r[1], "tokens": r[2], "seq": r[3]}
        for r in rows
    ]


def list_threads(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """按用户列出会话（thread_id / 最新问题 / 消息数 / 最后活跃时间），最后活跃倒序。

    供"聊天窗口左侧会话列表"使用（豆包式历史会话切换）；无会话返回空列表。
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT t.thread_id, t.last_question, t.msg_count, t.last_at
            FROM (
                SELECT c1.thread_id,
                       COUNT(*)                                AS msg_count,
                       MAX(c1.created_at)                      AS last_at,
                       (SELECT c2.content FROM conversation_messages c2
                         WHERE c2.thread_id = c1.thread_id
                           AND c2.role = 'user'
                         ORDER BY c2.seq DESC LIMIT 1)         AS last_question
                FROM conversation_messages c1
                WHERE c1.user_id = %s
                GROUP BY c1.thread_id
            ) t
            ORDER BY t.last_at DESC
            LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()
    return [
        {
            "thread_id": r[0],
            "last_question": r[1],
            "message_count": r[2],
            "last_active_at": r[3].isoformat() if r[3] else None,
        }
        for r in rows
    ]


def history_token_count(thread_id: str) -> int:
    """会话当前总 token（全部消息求和；压缩后旧消息已移除，summary 计入）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM conversation_messages WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def get_summary(thread_id: str) -> Optional[str]:
    """取最新一条压缩摘要（role=summary）；无则 None。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT content FROM conversation_messages "
            "WHERE thread_id = %s AND role = 'summary' ORDER BY seq DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
    return row[0] if row else None


def get_recent_turns(thread_id: str, n_turns: int) -> list[dict[str, Any]]:
    """取最近 n_turns 轮的 user/assistant 原文（不含 summary），按 seq 升序返回。

    一轮 = user + assistant 两条；末尾若 user 尚无 assistant（异常中断）也一并返回。
    """
    limit = max(1, n_turns) * 2
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content, tokens, seq FROM conversation_messages "
            "WHERE thread_id = %s AND role IN ('user', 'assistant') "
            "ORDER BY seq DESC LIMIT %s",
            (thread_id, limit),
        ).fetchall()
    items = [
        {"role": r[0], "content": r[1], "tokens": r[2], "seq": r[3]}
        for r in rows
    ]
    items.reverse()
    return items


# ---------------------------------------------------------------------------
# 上下文构造（注入 Manager/Decision）
# ---------------------------------------------------------------------------

def _format_turn(role: str, content: str) -> str:
    speaker = "用户" if role == "user" else "助手"
    return f"{speaker}：{content}"


def _load_parts(thread_id: str, keep: int) -> tuple[Optional[str], list[dict[str, Any]]]:
    """一次连接读取上下文素材：最新摘要 + 最近 keep 轮原文（避免两次连接）。"""
    limit = max(1, keep) * 2
    with connect() as conn:
        srow = conn.execute(
            "SELECT content FROM conversation_messages "
            "WHERE thread_id = %s AND role = 'summary' ORDER BY seq DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        rows = conn.execute(
            "SELECT role, content, tokens, seq FROM conversation_messages "
            "WHERE thread_id = %s AND role IN ('user', 'assistant') "
            "ORDER BY seq DESC LIMIT %s",
            (thread_id, limit),
        ).fetchall()
    summary = srow[0] if srow else None
    items = [
        {"role": r[0], "content": r[1], "tokens": r[2], "seq": r[3]}
        for r in rows
    ]
    items.reverse()
    return summary, items


def build_conversation_context(thread_id: str, keep_recent: Optional[int] = None) -> str:
    """构造注入 prompt 的会话上下文：历史摘要 + 最近 N 轮原文；无历史返回 ""。

    格式（示例）：
        【历史会话摘要】……
        【最近对话】
        用户：……
        助手：……
    """
    keep = keep_recent if keep_recent is not None else settings.CONVERSATION_KEEP_RECENT_TURNS
    summary, recent = _load_parts(thread_id, keep)
    if not summary and not recent:
        return ""

    parts: list[str] = []
    if summary:
        parts.append(f"【历史会话摘要】\n{summary}")
    if recent:
        turns = "\n".join(_format_turn(m["role"], m["content"]) for m in recent)
        parts.append(f"【最近对话】\n{turns}")
    return "\n\n".join(parts)

# ---------------------------------------------------------------------------
# 历史压缩（token 超 HISTORY_COMPRESSION_THRESHOLD 触发）
# ---------------------------------------------------------------------------

COMPRESS_SYSTEM = "你是会话历史压缩器，只输出压缩摘要，不做解释。"

COMPRESS_PROMPT = """请把以下多轮会话内容（可能已包含一段旧摘要）压缩为一段精炼的中文摘要，供后续对话承接上下文。

【待压缩内容】
{text}

压缩要求：
1. 保留：讨论过的主题与分析目标、关键实体（市场/品牌/品类/部门）、重要数据结论、
   用户的持续关注点、尚未回答或待跟进的问题；
2. 丢弃：寒暄、重复表述、过程性细节与无关内容；
3. 可用连贯短句或分点，确保信息不丢失、不新增；
4. 只输出压缩摘要本身。
"""


def _llm_compress(to_compress: list[dict[str, Any]]) -> str:
    """把待压缩消息交给 LLM 压成摘要；LLM 不可用时降级截断拼接（保证 token 能下降）。"""
    lines: list[str] = []
    for m in to_compress:
        if m["role"] == "summary":
            lines.append(f"[已有摘要] {m['content']}")
        else:
            lines.append(_format_turn(m["role"], m["content"]))
    blob = "\n".join(lines)

    if llm_available():
        from langchain_core.messages import HumanMessage, SystemMessage

        model = get_chat_model(tier="medium")
        messages = [
            SystemMessage(content=COMPRESS_SYSTEM),
            HumanMessage(content=COMPRESS_PROMPT.format(text=blob[:12000])),
        ]
        out = (invoke_text(model, messages, logger_name="conversation") or "").strip()
        if out:
            return out
    # 降级：无 LLM / 调用失败 → 硬截断，确保压缩仍能降低 token
    return "[历史摘要（降级截断）] " + blob[:2000]


def compress_history(thread_id: str, keep_recent: Optional[int] = None) -> dict[str, Any]:
    """压缩会话历史：保留最近 N 轮原文，其余（含旧摘要）压成一条新 summary。

    Returns:
        {"compressed": True, "removed", "kept", "tokens_before", "tokens_after"}；
        无内容可压缩时 {"compressed": False, "reason": "nothing_to_compress"}。
    """
    keep = keep_recent if keep_recent is not None else settings.COMPRESSION_KEEP_RECENT
    messages = get_messages(thread_id)
    ua = [m for m in messages if m["role"] in ("user", "assistant")]
    keep_count = keep * 2

    # 分界：保留最后 keep_count 条 user/assistant；更早的（及旧摘要）进压缩
    if len(ua) > keep_count:
        cut_seq = ua[-keep_count]["seq"]
        to_compress = [
            m for m in messages
            if m["role"] == "summary"
            or (m["role"] in ("user", "assistant") and m["seq"] < cut_seq)
        ]
    else:
        # user/assistant 全在保留范围，仅滚动旧摘要
        to_compress = [m for m in messages if m["role"] == "summary"]

    if not to_compress:
        return {"compressed": False, "reason": "nothing_to_compress"}

    tokens_before = history_token_count(thread_id)
    new_summary = _llm_compress(to_compress)
    remove_seqs = [m["seq"] for m in to_compress]

    with connect() as conn:
        conn.execute(
            "DELETE FROM conversation_messages WHERE thread_id = %s AND seq = ANY(%s)",
            (thread_id, remove_seqs),
        )
        maxseq = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM conversation_messages WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()[0]
        summary_tokens = count_tokens(new_summary)
        conn.execute(
            "INSERT INTO conversation_messages (thread_id, user_id, role, content, tokens, seq) "
            "VALUES (%s, %s, 'summary', %s, %s, %s)",
            (thread_id, "default", new_summary, summary_tokens, maxseq + 1),
        )
        conn.commit()

    tokens_after = history_token_count(thread_id)
    logger.info(
        "conversation.compressed",
        thread_id=thread_id,
        removed=len(remove_seqs),
        kept=min(len(ua), keep_count),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
    )
    return {
        "compressed": True,
        "removed": len(remove_seqs),
        "kept": min(len(ua), keep_count),
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
    }
