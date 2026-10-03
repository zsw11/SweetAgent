# -*- coding: utf-8 -*-
"""Streamlit Web UI（待办 12 / Phase 9 MVP + 记忆管理集成）。

启动方式（两个终端，先后端后 UI）：
    .venv\\Scripts\\uvicorn app.main:app --port 8000
    .venv\\Scripts\\streamlit run webui.py

功能：
1) 提问 -> 调 POST /chat -> 展示决策报告 / 各部门分析 / 路由信息；
   （/chat 后端内置"对话后自动沉淀记忆"钩子，无需 UI 重复触发）
2) 侧边栏记忆管理：
   - GET /memory 查看当前用户画像 / 偏好 / 非结构化记忆
   - POST /memory/extract 手动"立即沉淀本轮对话为记忆"（强制提取，绕过规则预筛）
3) 阶段状态中文化、置信度含义说明。
"""

from __future__ import annotations

import time
from typing import Any, Optional

import pandas as pd
import requests
import streamlit as st

DEFAULT_API = "http://localhost:8000"

# 阶段英文 -> 中文（后端 stage 值：planning/running/done/error/awaiting_feedback）
STAGE_LABELS = {
    "planning": "规划中",
    "running": "执行中",
    "done": "完成",
    "error": "出错",
    "awaiting_feedback": "等待确认（质量门）",
    "unknown": "未知",
}


# ---------------------------------------------------------------------------
# 后端交互
# ---------------------------------------------------------------------------
def api_base() -> str:
    return (st.sidebar.text_input("后端地址", DEFAULT_API) or DEFAULT_API).rstrip("/")


def check_health(base: str) -> dict[str, Any]:
    try:
        r = requests.get(f"{base}/health", timeout=5)
        r.raise_for_status()
        return {"ok": True, "data": r.json()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def ask(
    base: str,
    question: str,
    user_id: str = "default",
    human_in_the_loop: bool = False,
    thread_id: str = "",
) -> dict[str, Any]:
    """调用 POST /chat，返回 (ok, elapsed, data|error)。

    thread_id：多轮承接的关键——同一会话内传入已保存的 thread_id，
    后端据此注入 conversation_context（历史摘要+最近轮次），
    否则每次提问都生成新会话，历史问题就"看不到"了。

    human_in_the_loop=True 时，质量门自动回炉耗尽且答案仍不合格，
    后端返回 stage="awaiting_feedback"（候选答案 + issues），
    需再调 resume_chat 提交 approve/revise（考点二十九）。
    """
    t0 = time.time()
    try:
        payload: dict[str, Any] = {
            "question": question,
            "user_id": user_id,
            "human_in_the_loop": human_in_the_loop,
        }
        if thread_id:
            payload["thread_id"] = thread_id
        r = requests.post(f"{base}/chat", json=payload, timeout=300)
        elapsed = round(time.time() - t0, 1)
        r.raise_for_status()
        return {"ok": True, "elapsed": elapsed, "data": r.json()}
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:
            pass
        return {
            "ok": False,
            "elapsed": round(time.time() - t0, 1),
            "error": f"HTTP {exc.response.status_code}: {detail}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed": round(time.time() - t0, 1),
            "error": str(exc),
        }


def resume_chat(base: str, thread_id: str, action: str, feedback: str = "") -> dict[str, Any]:
    """POST /chat/{thread_id}/resume：质量门暂停后提交用户意见。

    action="approve"：接受候选答案放行 END；
    action="revise"：带 feedback（用户纠正意见）重新生成，需非空 feedback。
    """
    t0 = time.time()
    try:
        r = requests.post(
            f"{base}/chat/{thread_id}/resume",
            json={"action": action, "feedback": feedback},
            timeout=300,
        )
        elapsed = round(time.time() - t0, 1)
        r.raise_for_status()
        return {"ok": True, "elapsed": elapsed, "data": r.json()}
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:
            pass
        return {
            "ok": False,
            "elapsed": round(time.time() - t0, 1),
            "error": f"HTTP {exc.response.status_code}: {detail}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed": round(time.time() - t0, 1),
            "error": str(exc),
        }


def fetch_memory(base: str, user_id: str) -> dict[str, Any]:
    """GET /memory：查看用户全部记忆（画像 / 偏好 / 非结构化）。"""
    try:
        r = requests.get(f"{base}/memory", params={"user_id": user_id}, timeout=10)
        r.raise_for_status()
        return {"ok": True, "data": r.json()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def extract_memory(
    base: str, thread_id: str, user_id: str, question: str, answer: str
) -> dict[str, Any]:
    """POST /memory/extract：强制沉淀本轮对话为记忆（绕过规则预筛）。"""
    try:
        r = requests.post(
            f"{base}/memory/extract",
            json={
                "thread_id": thread_id,
                "user_id": user_id,
                "user_question": question,
                "final_answer": answer,
            },
            timeout=60,
        )
        r.raise_for_status()
        return {"ok": True, "data": r.json()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def send_feedback(
    base: str,
    thread_id: str,
    user_id: str,
    rating: str,
    question: str,
    answer: str,
    comment: str = "",
) -> dict[str, Any]:
    """POST /chat/feedback：用户点踩（rating=down）触发失败案例采集回流（考点六十六）。

    rating=down 时，后端把问题/回答（脱敏）写入 evaluation_harvest 采集池，
    开发人员可在管理台（/scheduler-ui 失败案例池）半自动转正为回归用例；
    rating=up 仅做满意度计数，不采集。
    """
    try:
        r = requests.post(
            f"{base}/chat/feedback",
            json={
                "thread_id": thread_id,
                "user_id": user_id,
                "rating": rating,
                "question": question,
                "answer": answer,
                "comment": comment,
            },
            timeout=30,
        )
        r.raise_for_status()
        return {"ok": True, "data": r.json()}
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {exc.response.status_code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 决策报告渲染
# ---------------------------------------------------------------------------
def _fmt_decision_item(label: str, item: dict[str, Any]) -> str:
    if label == "关键发现":
        return f"**[{item.get('category', '?')}]** {item.get('finding', '')}"
    if label == "根因分析":
        return f"{item.get('cause', '')}（证据：{item.get('evidence', '')}）"
    if label == "行动建议":
        return f"[{item.get('priority', '?')}] {item.get('action', '')}"
    if label == "风险提示":
        return f"[{item.get('severity', '?')}] {item.get('risk', '')}"
    return str(item)


def render_decision(decision: dict[str, Any]) -> None:
    st.subheader("📌 最终决策")
    st.write(decision.get("summary", "（无摘要）"))
    conf = decision.get("confidence")
    if conf is not None:
        st.metric("置信度", f"{conf:.2f}")
        st.caption(
            "置信度 0~1：由决策 Agent 依据【数据完整度 + 证据充分性】自评——"
            "多部门数据齐全时高（>0.7），仅单部门或关键数据缺失时低，属诚实降级而非答错。"
        )
    with st.expander("详细决策报告", expanded=False):
        for label, key in [
            ("关键发现", "findings"),
            ("根因分析", "root_causes"),
            ("行动建议", "recommendations"),
            ("风险提示", "risks"),
        ]:
            items = decision.get(key) or []
            if not items:
                continue
            st.markdown(f"**{label}**（{len(items)}）")
            for item in items:
                if isinstance(item, dict):
                    st.markdown(f"- {_fmt_decision_item(label, item)}")
                else:
                    st.markdown(f"- {item}")


# ---------------------------------------------------------------------------
# 部门分析渲染
# ---------------------------------------------------------------------------
def _metrics_df(metrics: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for m in metrics:
        rows.append(
            {
                "指标": m.get("name", ""),
                "前期": m.get("prev"),
                "近21天": m.get("last21"),
                "变化%": m.get("change_pct"),
            }
        )
    return pd.DataFrame(rows)


def _anomalies_df(anomalies: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for a in anomalies:
        rows.append(
            {
                "SKU": a.get("sku", ""),
                "指标": a.get("indicator", ""),
                "变化%": a.get("change_pct"),
                "说明": a.get("reason", ""),
            }
        )
    return pd.DataFrame(rows)


def render_department(name: str, res: dict[str, Any]) -> None:
    st.markdown(f"### {name}")
    st.write(res.get("summary", "（无摘要）"))
    conf = res.get("confidence")
    if conf is not None:
        st.caption(f"置信度 {conf:.2f} · 模式 {res.get('mode', '-')}")

    metrics = res.get("metrics") or []
    if metrics:
        st.markdown("**指标**")
        st.dataframe(_metrics_df(metrics), width="stretch", hide_index=True)

    anomalies = res.get("anomalies") or []
    if anomalies:
        st.markdown("**异常**")
        st.dataframe(_anomalies_df(anomalies), width="stretch", hide_index=True)

    observations = res.get("observations") or []
    if observations:
        with st.expander(f"数据查询（{len(observations)} 次）", expanded=False):
            for i, obs in enumerate(observations, 1):
                st.markdown(
                    f"**查询 {i}** · {obs.get('row_count', 0)} 行 · "
                    f"{obs.get('duration_ms', 0)} ms"
                )
                st.code(obs.get("sql", ""), language="sql")

    analysis = res.get("analysis") or []
    if analysis:
        with st.expander("分析过程", expanded=False):
            for line in analysis:
                st.write(line)


# ---------------------------------------------------------------------------
# 侧边栏：记忆管理
# ---------------------------------------------------------------------------
def render_memory_panel(base: str, user_id: str) -> None:
    """记忆管理：查看 + 手动沉淀（关窗写入的等价入口）。"""
    with st.sidebar.expander("🧠 记忆管理", expanded=False):
        mem = fetch_memory(base, user_id)
        if not mem["ok"]:
            st.error(f"读取记忆失败：{mem['error']}")
        else:
            d = mem["data"]
            profiles = d.get("profiles") or []
            prefs = d.get("preferences") or []
            mems = d.get("memories") or []
            st.markdown(f"**{user_id} 的记忆**")
            st.markdown(f"- 画像 {len(profiles)} · 偏好 {len(prefs)} · 非结构化 {len(mems)}")
            if profiles:
                with st.expander("画像", expanded=False):
                    for p in profiles:
                        conf = p.get("confidence")
                        st.write(
                            f"`{p.get('key')}` = {p.get('value')}"
                            + (f"（置信 {conf:.2f}）" if conf is not None else "")
                        )
                        if p.get("evidence"):
                            st.caption(f"依据：{p['evidence']}")
            if prefs:
                with st.expander("偏好", expanded=False):
                    for p in prefs:
                        st.write(f"`{p.get('key')}` = {p.get('value')}")
                        if p.get("evidence"):
                            st.caption(f"依据：{p['evidence']}")
            if mems:
                with st.expander("非结构化记忆", expanded=False):
                    for m in mems[:10]:
                        label = {
                            "preference": "偏好",
                            "fact": "事实",
                            "conclusion": "历史结论",
                            "rule": "规则",
                        }.get(m.get("memory_type"), m.get("memory_type") or "通用")
                        dept = m.get("department") or "通用"
                        st.write(f"`[{label}/{dept}]` {m.get('content')}")
            if st.button("🔄 刷新"):
                st.rerun()

        last = st.session_state.get("last")
        if st.button("💾 立即沉淀本轮对话为记忆"):
            if last is None:
                st.warning("还没有进行过对话，先提一个问题吧。")
            else:
                with st.spinner("正在提取记忆（真实 LLM）..."):
                    ex = extract_memory(
                        base, last["thread_id"], user_id,
                        last["question"], last["answer"],
                    )
                if ex["ok"]:
                    d = ex["data"]
                    if d.get("triggered"):
                        st.success(
                            f"已沉淀：画像 {d.get('profiles', 0)} · "
                            f"偏好 {d.get('preferences', 0)} · "
                            f"非结构化 {d.get('memories', 0)}"
                        )
                    else:
                        st.info(f"本轮无可提取记忆（{d.get('reason', '未触发')}）")
                else:
                    st.error(f"沉淀失败：{ex['error']}")


# ---------------------------------------------------------------------------
# 主页面（豆包式聊天窗口：左侧会话列表 + 主区消息流）
# ---------------------------------------------------------------------------
def _current_thread() -> str:
    return st.session_state.get("thread_id") or ""


def _append_msg(role: str, content: str, detail: Optional[dict[str, Any]] = None) -> None:
    """往当前会话消息流追加一条（含可选完整响应 detail，用于历史重渲染详细报告）。"""
    msgs = st.session_state.setdefault("messages", [])
    msgs.append({"role": role, "content": content, "detail": detail})
    st.session_state["messages"] = msgs


def _compose_answer_text(data: dict[str, Any]) -> str:
    """组装助手消息正文（聊天窗口主文案）：最终答案 + 决策摘要。"""
    parts = [data.get("final_answer", "")]
    decision = data.get("decision_result") or {}
    if decision.get("summary"):
        parts.append(f"**决策摘要**：{decision['summary']}")
    return "\n\n".join(p for p in parts if p) or "（无回答）"


def render_feedback_bar(base: str, uid: str, q: str, answer: str, seq: int) -> None:
    """回答反馈（点踩回流，考点六十六）——状态机写法，避免 Streamlit 嵌套按钮 bug。

    点「👎 没用」只置 session_state 标志并 rerun；评论框与提交按钮在标志位
    为真时于同一层渲染，提交后才真正调 /chat/feedback（rating=down → 落采集池）。
    """
    fb_key = f"fb_{seq}"
    st.caption("这个回答对你有帮助吗？")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("👍 有用", key=f"{fb_key}_up"):
            res = send_feedback(base, _current_thread(), uid, "up", q, answer)
            if res["ok"]:
                st.success("已记录 👍（用于满意度统计）")
            else:
                st.error(f"反馈失败：{res['error']}")
    with c2:
        if st.button("👎 没用", key=f"{fb_key}_down"):
            st.session_state[f"{fb_key}_pending"] = True

    if st.session_state.get(f"{fb_key}_pending"):
        comment = st.text_input(
            "哪里不对？（可选，帮助改进）",
            key=f"{fb_key}_comment",
            placeholder="例如：没给具体数字 / 数据看起来不对",
        )
        if st.button("提交 👎 反馈", key=f"{fb_key}_submit"):
            res = send_feedback(base, _current_thread(), uid, "down", q, answer,
                                comment.strip())
            st.session_state.pop(f"{fb_key}_pending", None)
            if res["ok"]:
                st.warning("已采集 👎，开发人员可在【定时任务管理台 /scheduler-ui 失败案例池】处理转正")
            else:
                st.error(f"反馈失败：{res['error']}")


def render_answer_card(base: str, resp: dict[str, Any], data: dict[str, Any], q: str,
                       uid: str, seq: int) -> None:
    """渲染一条完整回答（决策报告 / 各部门 / 路由）+ 反馈条。"""
    stage_zh = STAGE_LABELS.get(data["stage"], data["stage"])
    st.caption(
        f"耗时 {resp['elapsed']}s · 阶段：{stage_zh} · "
        f"会话 {data['thread_id'][:8]}… · 用户 {uid}"
    )
    render_decision(data["decision_result"])

    st.divider()
    st.subheader("🏢 各部门分析")
    depts = data.get("department_results") or {}
    if depts:
        tabs = st.tabs(list(depts.keys()))
        for tab, name in zip(tabs, depts.keys()):
            with tab:
                render_department(name, depts[name])
    else:
        st.info("本轮没有部门被调度。")

    with st.expander("路由与任务信息", expanded=False):
        st.write("**规划部门**：", data.get("required_agents"))
        st.write("**已完成任务**：", data.get("completed_tasks"))
        st.write("**跳过任务**：", data.get("skipped_tasks"))

    st.divider()
    render_feedback_bar(base, uid, q, data.get("final_answer", ""), seq)

    st.caption(
        "💡 记忆说明：/chat 后端已内置【对话后自动沉淀记忆】钩子（规则+历史阈值触发，"
        "零额外费用）；如未自动触发，可在左侧【记忆管理】点【立即沉淀本轮对话为记忆】。"
    )


def render_message_flow(base: str, uid: str) -> None:
    """按消息流渲染当前会话全部消息（豆包式聊天窗口）。

    用户消息 = 气泡；助手消息 = 答案卡片（有完整 detail 时展开详细报告，
    历史会话回读只有文本内容则只显示正文）。
    反馈条的问题取该回答之前最近一条用户消息（历史会话无 question 字段时）。
    """
    msgs: list[dict[str, Any]] = st.session_state.get("messages") or []
    last_question = ""
    for i, m in enumerate(msgs):
        if m["role"] == "user":
            last_question = m["content"]
            with st.chat_message("user"):
                st.write(m["content"])
        else:
            q = m.get("question") or last_question
            with st.chat_message("assistant"):
                st.markdown(m["content"])
                detail = m.get("detail")
                if detail:
                    resp, data = detail.get("resp"), detail.get("data")
                    if resp and data:
                        with st.expander("查看完整决策报告（详情）", expanded=False):
                            render_answer_card(base, resp, data, q, uid, i)
                else:
                    # 历史会话回读：无完整响应，仅展示文本 + 反馈条
                    st.divider()
                    render_feedback_bar(base, uid, q, m["content"], i)


def render_thread_list(base: str, uid: str) -> None:
    """左侧会话列表（新会话 + 历史切换）。"""
    st.sidebar.markdown("### 💬 会话")
    if st.sidebar.button("➕ 新会话", use_container_width=True, key="new_thread"):
        st.session_state["thread_id"] = None
        st.session_state["messages"] = []
        st.session_state.pop("last", None)
        st.rerun()

    try:
        r = requests.get(f"{base}/chat/threads", params={"user_id": uid}, timeout=10)
        r.raise_for_status()
        threads = r.json().get("items") or []
    except Exception as exc:
        st.sidebar.caption(f"会话列表加载失败：{exc}")
        return

    if not threads:
        st.sidebar.caption("暂无历史会话")
        return

    cur = _current_thread()
    for t in threads:
        label = f"{t['last_question'] or '（空）'}\n{t['message_count']} 条"
        if st.sidebar.button(label, key=f"thr_{t['thread_id']}",
                             use_container_width=True):
            try:
                r = requests.get(f"{base}/chat/{t['thread_id']}/messages", timeout=10)
                r.raise_for_status()
                raw = r.json().get("messages") or []
            except Exception as exc:
                st.sidebar.error(f"读取会话失败：{exc}")
                continue
            st.session_state["thread_id"] = t["thread_id"]
            st.session_state["messages"] = [
                {"role": m["role"], "content": m["content"], "detail": None}
                for m in raw
            ]
            st.session_state.pop("last", None)
            st.rerun()


def render_quality_pending(base: str, data: dict[str, Any], uid: str, q: str) -> None:
    """质量门暂停面板（human-in-the-loop）：展示候选答案 + issues，供用户 approve / revise。

    对应后端 stage="awaiting_feedback"（quality_pending 含 draft_answer / issues），
    approve -> 放行 END；revise -> 带 feedback 重新生成（考点二十九）。
    """
    st.warning("⚠️ 质量门：答案经自动回炉（≤2 次）仍不合格，已暂停等待你确认或纠正。")
    thread_id = data["thread_id"]
    st.caption(f"会话 {thread_id[:8]}… · 用户 {uid}")
    pending = data.get("quality_pending") or {}
    st.markdown("**候选答案（草稿）**")
    st.info(pending.get("draft_answer") or "（空）")
    issues = pending.get("issues") or []
    if issues:
        st.markdown(f"**质量门诊断（{len(issues)}）**")
        for it in issues:
            st.markdown(f"- {it}")
    st.markdown("**请选择：接受该答案，或输入纠正意见后重新生成**")

    col1, col2 = st.columns([1, 2])
    with col1:
        if st.button("✅ 接受答案", key=f"approve_{thread_id[:8]}"):
            resp = resume_chat(base, thread_id, "approve")
            _handle_resume_result(base, resp, q, uid)
    with col2:
        feedback = st.text_input(
            "纠正意见（revise 必填）",
            key=f"fb_{thread_id[:8]}",
            placeholder="例如：请补充库存因素，并正面回答销量下滑的具体原因",
        )
        if st.button("✏️ 提交修改并重新生成", key=f"revise_{thread_id[:8]}"):
            if not (feedback or "").strip():
                st.error('action="revise" 时必须提供非空 feedback')
            else:
                resp = resume_chat(base, thread_id, "revise", feedback.strip())
                _handle_resume_result(base, resp, q, uid)


def _handle_resume_result(base: str, resp: dict[str, Any], q: str, uid: str) -> None:
    """resume 返回后统一处理：成功则渲染结果；若再次暂停（re-revise 后仍不合格）则继续等待。"""
    if not resp["ok"]:
        st.error(f"恢复执行失败：{resp['error']}")
        return
    data = resp["data"]
    if data.get("stage") == "awaiting_feedback":
        render_quality_pending(base, data, uid, q)
    else:
        st.success("✅ 已按你的意见恢复执行完成")
        _append_msg("assistant", _compose_answer_text(data),
                    detail={"resp": resp, "data": data})
        st.session_state["thread_id"] = data["thread_id"]
        st.session_state["last"] = {
            "thread_id": data["thread_id"],
            "question": q,
            "answer": _compose_answer_text(data),
        }
        st.rerun()


def main() -> None:
    st.set_page_config(page_title="SweetNight Agent 工作台", layout="wide")
    # 聊天窗口样式：用户消息右对齐蓝气泡、助手左对齐浅灰气泡、圆角+头像
    st.markdown(
        """
        <style>
        .stChatMessage { display: flex; margin-bottom: 12px; }
        [data-testid="stChatMessage"] { border-radius: 12px; }
        [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatar"] svg[data-testid="chatAvatarIcon-user"]) {
            flex-direction: row-reverse;
        }
        [data-testid="stChatMessageAvatar"] { margin: 4px 10px 0 0; }
        [data-testid="stChatMessageContent"] p { margin: 0; line-height: 1.55; }
        .stChatMessageContent { border-radius: 12px; padding: 10px 14px; }
        [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) [data-testid="stChatMessageContent"] {
            background: #1c7ed6; color: #fff; border-radius: 14px 4px 14px 14px;
        }
        [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) [data-testid="stChatMessageContent"] {
            background: #f1f3f5; color: #1a1a1a; border-radius: 4px 14px 14px 14px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("SweetNight 跨境电商 AI Agent 工作台")
    st.caption(
        "Manager 规划 → 部门 Agent 分析（Operation/Finance/Logistics/Product）→ Decision 决策汇总"
    )

    base = api_base()
    user_id = st.sidebar.text_input("用户 ID", "default")
    uid = user_id.strip() or "default"
    hilt = st.sidebar.checkbox(
        "质量门人工确认（human-in-the-loop）",
        value=False,
        help="答案经自动回炉仍不合格时，暂停等待你确认或输入纠正意见（考点二十九）；"
        "关闭时不合格答案直接放行（非交互）。",
    )

    health = check_health(base)
    if health["ok"]:
        st.sidebar.success("✅ 后端已连接")
    else:
        st.sidebar.error(f"后端未连接：{health['error']}")
        st.sidebar.caption("请先启动后端：.venv\\Scripts\\uvicorn app.main:app --port 8000")

    # 管理台入口（定时任务管理台 / 失败案例池 / 半自动转正）
    st.sidebar.markdown("### ⏰ 管理台")
    admin_base = base
    st.sidebar.link_button("📋 定时任务管理台（含失败案例池）",
                           f"{admin_base}/scheduler-ui", use_container_width=True)
    st.sidebar.caption("任务审核 · 失败案例池 · 半自动转正 都在这里")

    # 左侧会话列表（新会话 + 历史切换）
    render_thread_list(base, uid)
    render_memory_panel(base, uid)

    # 主区：豆包式消息流（历史 + 当前会话）
    render_message_flow(base, uid)

    # 提问区
    with st.form("ask_form"):
        question = st.text_area(
            "你的问题",
            height=100,
            placeholder="例如：分析 SweetNight 品牌美国市场过去90天各SKU的GMV、订单、销量变化，并找出异常SKU",
        )
        submitted = st.form_submit_button("🚀 执行分析", type="primary")

    if submitted:
        q = question.strip()
        if not q:
            st.warning("请输入问题。")
        else:
            # 先落用户消息，再真实调用（失败也保留问题气泡）
            _append_msg("user", q)
            with st.spinner("Agent 执行中（真实调用多部门 LLM，预计 10~60 秒）..."):
                resp = ask(base, q, uid, human_in_the_loop=hilt,
                           thread_id=_current_thread())
            if not resp["ok"]:
                _append_msg("assistant", f"⚠️ 执行失败：{resp['error']}")
                st.rerun()
            else:
                data = resp["data"]
                st.session_state["thread_id"] = data["thread_id"]
                if data.get("stage") == "awaiting_feedback":
                    # 质量门暂停：渲染候选答案 + 审批面板，等待用户 approve/revise
                    # （不 rerun，否则面板会被刷新掉；用户操作走 resume 端点）
                    _append_msg(
                        "assistant",
                        "⚠️ 质量门：答案经自动回炉仍不合格，已暂停等待你确认或纠正（见下方面板）。",
                        detail={"resp": resp, "data": data},
                    )
                    render_quality_pending(base, data, uid, q)
                else:
                    _append_msg("assistant", _compose_answer_text(data),
                                detail={"resp": resp, "data": data})
                    st.session_state["last"] = {
                        "thread_id": data["thread_id"],
                        "question": q,
                        "answer": _compose_answer_text(data),
                    }
                    st.rerun()


if __name__ == "__main__":
    main()
