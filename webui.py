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

# 阶段英文 -> 中文（后端 stage 值：planning/running/done/error）
STAGE_LABELS = {
    "planning": "规划中",
    "running": "执行中",
    "done": "完成",
    "error": "出错",
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


def ask(base: str, question: str, user_id: str = "default") -> dict[str, Any]:
    """调用 POST /chat，返回 (ok, elapsed, data|error)。"""
    t0 = time.time()
    try:
        r = requests.post(
            f"{base}/chat",
            json={"question": question, "user_id": user_id},
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
# 主页面
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="SweetNight Agent 工作台", layout="wide")
    st.title("SweetNight 跨境电商 AI Agent 工作台")
    st.caption(
        "Manager 规划 → 部门 Agent 分析（Operation/Finance/Logistics/Product）→ Decision 决策汇总"
    )

    base = api_base()
    user_id = st.sidebar.text_input("用户 ID", "default")
    uid = user_id.strip() or "default"

    health = check_health(base)
    if health["ok"]:
        st.sidebar.success("✅ 后端已连接")
    else:
        st.sidebar.error(f"后端未连接：{health['error']}")
        st.sidebar.caption("请先启动后端：.venv\\Scripts\\uvicorn app.main:app --port 8000")

    render_memory_panel(base, uid)

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
            with st.spinner("Agent 执行中（真实调用多部门 LLM，预计 10~60 秒）..."):
                resp = ask(base, q, uid)
            if not resp["ok"]:
                st.error(f"执行失败：{resp['error']}")
            else:
                data = resp["data"]
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

                # 记录本轮，供"立即沉淀"使用（/chat 内置钩子已自动尝试提取）
                st.session_state["last"] = {
                    "thread_id": data["thread_id"],
                    "question": q,
                    "answer": " ".join(
                        filter(
                            None,
                            [
                                data.get("final_answer", ""),
                                str((data.get("decision_result") or {}).get("summary", "")),
                            ],
                        )
                    ),
                }
                st.caption(
                    "💡 记忆说明：/chat 后端已内置【对话后自动沉淀记忆】钩子（规则+历史阈值触发，"
                    "零额外费用）；如未自动触发，可在左侧【记忆管理】点【立即沉淀本轮对话为记忆】。"
                )


if __name__ == "__main__":
    main()
