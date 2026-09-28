# -*- coding: utf-8 -*-
"""OPT-12 多轮会话验证：历史存储 / 上下文构造 / 压缩 / query 改写 / 全链集成 / 开关。

场景：
  1. token 计数（tiktoken / 空文本）
  2. 存储 CRUD（append 自增 seq、get 顺序、recent 轮数、token 求和）
  3. 上下文构造（无历史→""；有→最近对话；summary→摘要段）
  4. 压缩（多轮→保留最近 N 轮 + summary，token 下降；二次压缩滚动旧摘要）
  5. query 改写（无上下文→原文；有上下文→展开指代；清洗前缀/引号）
  6. 全链集成（mock 跑两轮：首轮独立、次轮承接上文并改写、历史落库）
  7. 开关关闭（不读/不写历史）

运行：.venv\\Scripts\\python.exe scripts\\verify_conversation.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


# ---------------------------------------------------------------------------
# 场景 1：token 计数
# ---------------------------------------------------------------------------
def test_tokens() -> None:
    from app.memory.conversation import count_tokens

    print("[1] token 计数")
    check("空文本 0", count_tokens("") == 0)
    check("英文 >0", count_tokens("Hello world, this is a test.") > 0)
    check("中文 >0", count_tokens("美国床垫退货率分析") > 0)
    check("更长文本 token 更多",
          count_tokens("美国床垫退货率分析 " * 10) > count_tokens("美国床垫退货率分析"))


# ---------------------------------------------------------------------------
# 场景 2：存储 CRUD
# ---------------------------------------------------------------------------
T1 = f"t_conv_crud_{uuid.uuid4().hex[:8]}"


def test_crud() -> None:
    from app.memory.conversation import (
        append_message,
        get_messages,
        get_recent_turns,
        history_token_count,
    )

    print("[2] 存储 CRUD")
    r0 = append_message(T1, "user", "美国床垫退货率怎么样？", user_id="u1")
    r1 = append_message(T1, "assistant", "退货率环比上升 2%，主要来自美国站。", user_id="u1")
    check("seq 从 0 自增", r0["seq"] == 0 and r1["seq"] == 1, f"got {r0['seq']},{r1['seq']}")
    check("user 消息 token>0", r0["tokens"] > 0)

    msgs = get_messages(T1)
    check("get 返回 2 条且有序", len(msgs) == 2 and [m["seq"] for m in msgs] == [0, 1])
    check("role 正确", msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant")

    check("recent 1 轮=2 条", len(get_recent_turns(T1, 1)) == 2)
    check("token 求和>0", history_token_count(T1) == r0["tokens"] + r1["tokens"])

    # append_turn：一个事务写一轮（user+assistant）+ 返回 history_tokens
    from app.memory.conversation import append_turn

    T1b = f"t_conv_turn_{uuid.uuid4().hex[:8]}"
    r = append_turn(T1b, "问题A", "答案A", user_id="u1")
    check("append_turn seq 连续", r["user_seq"] == 0 and r["assistant_seq"] == 1, str(r))
    check("append_turn 返回 history_tokens>0", r["history_tokens"] > 0, str(r))
    msgs = get_messages(T1b)
    check("append_turn 落 2 条且角色正确", len(msgs) == 2
          and msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant", str(msgs))
    check("append_turn 与 SUM 一致", history_token_count(T1b) == r["history_tokens"])
    _cleanup([T1b])


# ---------------------------------------------------------------------------
# 场景 3：上下文构造
# ---------------------------------------------------------------------------
def test_context() -> None:
    from app.memory.conversation import build_conversation_context

    print("[3] 上下文构造")
    empty = f"t_conv_empty_{uuid.uuid4().hex[:8]}"
    check("无历史→空串", build_conversation_context(empty) == "")

    ctx = build_conversation_context(T1)
    check("含最近对话段", "【最近对话】" in ctx and "用户" in ctx and "助手" in ctx, ctx[:80])
    check("含本轮内容", "退货率" in ctx)


# ---------------------------------------------------------------------------
# 场景 4：压缩
# ---------------------------------------------------------------------------
T2 = f"t_conv_compress_{uuid.uuid4().hex[:8]}"


def test_compression() -> None:
    from unittest.mock import patch

    from app.memory import conversation as cv
    from app.memory.conversation import (
        append_message,
        build_conversation_context,
        compress_history,
        get_messages,
        get_recent_turns,
        get_summary,
        history_token_count,
    )

    print("[4] 历史压缩（fake LLM 真正压缩）")
    # 造 5 轮（10 条）
    for i in range(5):
        append_message(T2, "user", f"第{i+1}轮问题，关于市场{i+1}的分析", user_id="u2")
        append_message(T2, "assistant", f"第{i+1}轮结论：市场{i+1}表现正常，指标{i+1}稳定", user_id="u2")

    before = history_token_count(T2)
    short_summary = "已讨论市场1至市场5，各市场指标均稳定，无异常。"
    # fake LLM：返回真正简短的摘要（不依赖真实模型）
    _llm_patches = [
        patch.object(cv, "llm_available", lambda: True),
        patch.object(cv, "get_chat_model", lambda tier="medium": object()),
        patch.object(cv, "invoke_text", lambda *a, **k: short_summary),
    ]
    for p in _llm_patches:
        p.start()
    try:
        r = compress_history(T2, keep_recent=2)   # 保留最近 2 轮=4 条
    finally:
        for p in _llm_patches:
            p.stop()

    check("compressed=True", r.get("compressed") is True, str(r))
    check("移除 6 条旧消息", r.get("removed") == 6, f"removed={r.get('removed')}")
    remaining = get_messages(T2)
    check("剩余=4原文+1摘要", len(remaining) == 5, f"got {len(remaining)}")
    check("保留最近 2 轮原文", len(get_recent_turns(T2, 2)) == 4)
    check("产生 summary", get_summary(T2) == short_summary)
    check("token 下降", r.get("tokens_after", before) < before,
          f"{r.get('tokens_after')} !< {before}")

    ctx = build_conversation_context(T2, keep_recent=2)
    check("上下文含摘要段", "【历史会话摘要】" in ctx, ctx[:80])
    check("上下文含最近对话段", "【最近对话】" in ctx)

    # 二次压缩：ua=4 全在保留范围，仅滚动旧 summary
    for p in _llm_patches:
        p.start()
    try:
        r2 = compress_history(T2, keep_recent=2)
    finally:
        for p in _llm_patches:
            p.stop()
    check("二次压缩滚动摘要", r2.get("compressed") is True and r2.get("removed") == 1, str(r2))
    check("二次后仍 5 条", len(get_messages(T2)) == 5)

    # 降级路径（无 LLM）：硬截断仍能产出 summary，保证压缩机制不依赖模型
    with patch.object(cv, "llm_available", lambda: False):
        fallback = cv._llm_compress([
            {"role": "user", "content": "某条较长的历史消息内容" * 20},
            {"role": "assistant", "content": "对应的历史结论内容" * 20},
        ])
    check("降级摘要非空且带标记", fallback.startswith("[历史摘要"), fallback[:30])


# ---------------------------------------------------------------------------
# 场景 5：query 改写
# ---------------------------------------------------------------------------
def test_rewrite() -> None:
    from unittest.mock import patch

    from app.memory import rewrite as rw
    from app.memory.rewrite import rewrite_question

    print("[5] query 改写")
    # 无上下文 → 原文（不调 LLM）
    check("无上下文→原文", rewrite_question("那物流呢", "") == "那物流呢")

    ctx = "【最近对话】\n用户：美国床垫退货率怎么样\n助手：退货率上升 2%"
    with patch.object(rw, "llm_available", lambda: True), \
         patch.object(rw, "get_chat_model", lambda tier="small": object()), \
         patch.object(rw, "invoke_text", lambda *a, **k: "美国床垫的物流时效与退货率对比"):
        out = rewrite_question("那物流呢", ctx)
    check("有上下文→展开指代", out == "美国床垫的物流时效与退货率对比", out)

    # 清洗前缀 + 引号
    with patch.object(rw, "llm_available", lambda: True), \
         patch.object(rw, "get_chat_model", lambda tier="small": object()), \
         patch.object(rw, "invoke_text", lambda *a, **k: "改写后：美国床垫物流分析"):
        out2 = rewrite_question("那物流呢", ctx)
    check("清洗前缀", out2 == "美国床垫物流分析", out2)

    # LLM 不可用 → 原文
    with patch.object(rw, "llm_available", lambda: False):
        check("LLM 不可用→原文", rewrite_question("那物流呢", ctx) == "那物流呢")


# ---------------------------------------------------------------------------
# 场景 6 + 7：全链集成 + 开关
# ---------------------------------------------------------------------------
def test_integration_and_switch() -> None:
    import contextlib
    from unittest.mock import patch

    from app.graph.main_graph import run_question

    print("[6] 全链集成：两轮对话（mock）")
    T3 = f"t_conv_chain_{uuid.uuid4().hex[:8]}"

    class _MockManager:
        def __init__(self):
            self.calls: list[dict] = []

        def run(self, user_question, memory=None, injection_warning="", conversation_context=""):
            self.calls.append({"q": user_question, "ctx": conversation_context})
            return {
                "intent": "business_analysis",
                "required_agents": ["operation"],
                "tasks": [
                    {"id": "operation_analysis", "agent": "operation", "depends_on": [], "description": "分析"},
                    {"id": "decision", "agent": "decision", "depends_on": ["operation_analysis"], "description": "汇总"},
                ],
            }

    class _MockDecision:
        def __init__(self):
            self.calls: list[dict] = []

        def run(self, user_question, department_results, memory=None, feedback=None,
                injection_warning="", conversation_context=""):
            self.calls.append({"q": user_question, "ctx": conversation_context})
            # summary 加长，确保过质量门最短长度，避免回炉导致 Decision 多次调用、索引错位
            return {
                "summary": f"答案：{user_question}。以上为基于部门数据的分析结论。",
                "findings": [], "root_causes": [], "recommendations": [], "risks": [],
                "confidence": 0.9,
            }

    def _dept_factory(agent_name, context_builder=None):
        def node(state):
            return {
                "department_results": {agent_name: {"summary": f"{agent_name}结果", "confidence": 0.9}},
                "completed_tasks": [f"{agent_name}_analysis"],
            }
        return node

    mgr = _MockManager()
    dec = _MockDecision()
    rewrite_calls: list[dict] = []
    extract_tokens: list[int] = []

    def _fake_rewrite(q, ctx):
        rewrite_calls.append({"q": q, "ctx": ctx})
        return "美国床垫的物流时效分析" if ctx else q

    def _fake_maybe_extract(user_id, thread_id, user_question, final_answer, history_tokens=0):
        extract_tokens.append(history_tokens)

    patches = [
        patch("app.graph.main_graph.init_langsmith", lambda: None),
        patch("app.memory.checkpoint.get_checkpointer", lambda: None),
        patch("app.graph.main_graph.build_manager_memory", lambda *a, **k: None),
        patch("app.graph.main_graph.ManagerAgent", lambda: mgr),
        patch("app.graph.main_graph.DecisionAgent", lambda: dec),
        patch("app.graph.main_graph.make_department_node", _dept_factory),
        patch("app.memory.rewrite.rewrite_question", _fake_rewrite),
        patch("app.memory.extractor.maybe_extract_memories", _fake_maybe_extract),
    ]
    for p in patches:
        p.start()
    try:
        # 第 1 轮
        run_question("美国床垫退货率怎么样", thread_id=T3)
        check("首轮不调用改写", len(rewrite_calls) == 0, f"{rewrite_calls}")
        check("首轮 Manager 无历史上下文", mgr.calls[0]["ctx"] == "", f"'{mgr.calls[0]['ctx']}'")

        # 第 2 轮（追问）
        run_question("那物流呢", thread_id=T3)
        check("次轮调用改写", len(rewrite_calls) == 1 and bool(rewrite_calls[0]["ctx"]))
        check("次轮 Manager 收到历史上下文", mgr.calls[1]["ctx"] != "", "空")
        check("次轮问题已改写", mgr.calls[1]["q"] == "美国床垫的物流时效分析", mgr.calls[1]["q"])
        check("Decision 也收到历史", dec.calls[1]["ctx"] != "")

        from app.memory.conversation import get_messages
        check("历史落库 4 条", len(get_messages(T3)) == 4, f"{len(get_messages(T3))}")
        check("提取收到真实历史token(非0)", len(extract_tokens) == 2 and all(x > 0 for x in extract_tokens),
              f"{extract_tokens}")
        check("历史token随轮次增长", extract_tokens[1] > extract_tokens[0], f"{extract_tokens}")

        # ---- 场景 7：开关关闭 ----
        print("[7] 开关关闭")
        T4 = f"t_conv_off_{uuid.uuid4().hex[:8]}"
        from app.config.settings import settings
        orig = settings.CONVERSATION_HISTORY_ENABLED
        settings.CONVERSATION_HISTORY_ENABLED = False
        try:
            run_question("任意问题", thread_id=T4)
        finally:
            settings.CONVERSATION_HISTORY_ENABLED = orig
        from app.memory.conversation import get_messages as gm
        check("关闭后不落历史", len(gm(T4)) == 0, f"{len(gm(T4))}")
        check("关闭后首轮 Manager 无上下文", mgr.calls[2]["ctx"] == "")
    finally:
        for p in patches:
            p.stop()

    # 清理本脚本产生的会话
    _cleanup([T1, T2, T3, T4])


# ---------------------------------------------------------------------------
# 场景 8：记忆提取——历史 token 阈值触发（二期）
# ---------------------------------------------------------------------------
def test_extract_threshold() -> None:
    from unittest.mock import patch

    from app.memory import extractor as ex

    print("[8] 记忆提取：历史 token 阈值触发")
    # 问题/回答均不含规则关键词，触发与否只取决于 history_tokens
    q, a = "数据行数核对", "核对完成"
    with patch.object(ex, "_extract_once", lambda *x, **k: {"triggered": True}):
        r_hit = ex.maybe_extract_memories("u", "t", q, a, history_tokens=16000)  # >= 阈值
        r_miss = ex.maybe_extract_memories("u", "t", q, a, history_tokens=100)   # < 阈值
    check("达阈值触发提取", r_hit.get("triggered") is True, str(r_hit))
    check("未达阈值不触发", r_miss == {"triggered": False, "reason": "no_signal"}, str(r_miss))


def _cleanup(thread_ids: list[str]) -> None:
    import psycopg

    from app.config.settings import settings
    with psycopg.connect(settings.DATABASE_URL) as conn:
        for tid in thread_ids:
            conn.execute("DELETE FROM conversation_messages WHERE thread_id = %s", (tid,))
        conn.commit()
    print(f"  (已清理 {len(thread_ids)} 个测试会话)")


def main() -> None:
    test_tokens()
    test_crud()
    test_context()
    test_compression()
    test_rewrite()
    test_integration_and_switch()
    test_extract_threshold()
    print(f"\n结果: PASS {PASS} / FAIL {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
