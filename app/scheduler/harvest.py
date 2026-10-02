"""失败案例采集池（用户点踩回流，开发日志考点六十六）。

设计决策（已与用户确认，2026-10-02）：
- 只采用户点踩（user_downvote），不做 quality_gate / 硬异常自动采集——
  quality_gate 防"机器认为不行"，用户点踩是"用户认为不行"，后者才是评估该回归的信号
- 落库前脱敏：question/answer 走 app.security.masking，PII 不出库
- 幂等去重：同用户同问题同信号只采一条（uq_harvest_dedup），防刷屏
- 半自动转正：LLM 预填 expected_* 草稿（只做建议）→ 开发人员确认后插入
  evaluation_cases（category='online'）——防止"以错为正"固化错误期望值
- 转正后 eval_regression 的 load_cases 自动带上，评估逻辑零改动
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg.types.json import Jsonb

from app.config.settings import settings
from app.memory.db import connect, resolve_user_id
from app.observability.logging import get_logger

logger = get_logger("scheduler.harvest")


# ============================================================
# 采集（用户点踩入口）
# ============================================================

def harvest_downvote(
    user_id: str,
    thread_id: str,
    question: str,
    answer: str,
    *,
    run_id: Optional[str] = None,
    trace_url: Optional[str] = None,
    comment: Optional[str] = None,
) -> dict[str, Any]:
    """保存一条用户点踩案例（幂等：同人同题重复点踩只保留第一条）。

    脱敏在 API 层（调用方）完成后再传入本函数；此处不再重复脱敏。
    """
    uid = resolve_user_id(user_id)
    try:
        with connect() as conn:
            dup = conn.execute(
                "SELECT id FROM evaluation_harvest "
                "WHERE user_id=%s AND question=%s AND signal_type='user_downvote'",
                (uid, question),
            ).fetchone()
            if dup:
                return {"ok": False, "reason": "duplicate", "harvest_id": dup[0]}
            hid = conn.execute(
                "INSERT INTO evaluation_harvest "
                "(user_id, thread_id, question, answer, run_id, trace_url, comment) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (uid, thread_id, question, answer[:8000], run_id, trace_url,
                 (comment or "")[:2000]),
            ).fetchone()[0]
        logger.info("harvest.collected", harvest_id=hid, user_id=user_id,
                    thread_id=thread_id, question=question[:60])
        return {"ok": True, "harvest_id": hid}
    except Exception as exc:
        logger.warning("harvest.collect.fail", user_id=user_id, error=str(exc))
        return {"ok": False, "reason": str(exc)}


def list_harvest(status: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
    """列出采集池（默认 pending，可指定 status）。"""
    sql = "SELECT id, user_id, thread_id, question, answer, run_id, trace_url, comment, " \
          "signal_type, status, promoted_case_id, created_at FROM evaluation_harvest"
    conds, args = [], []
    if status:
        conds.append("status = %s")
        args.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY created_at DESC LIMIT %s"
    args.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [
        {
            "id": r[0], "user_id": r[1], "thread_id": r[2], "question": r[3],
            "answer": r[4], "run_id": r[5], "trace_url": r[6], "comment": r[7],
            "signal_type": r[8], "status": r[9], "promoted_case_id": r[10],
            "created_at": r[11].isoformat() if r[11] else None,
        }
        for r in rows
    ]


# ============================================================
# 半自动转正（LLM 预填草稿 + 人工确认）
# ============================================================

def draft_expected(harvest_id: int) -> dict[str, Any]:
    """LLM 根据点踩案例预填 expected_* 草稿（只做建议，不直接入库）。"""
    from app.llm import get_chat_model
    from app.llm.structured import invoke_structured
    from langchain_core.messages import HumanMessage, SystemMessage
    from pydantic import BaseModel, Field

    with connect() as conn:
        row = conn.execute(
            "SELECT question, answer FROM evaluation_harvest WHERE id=%s", (harvest_id,)
        ).fetchone()
    if row is None:
        return {"ok": False, "reason": "not_found"}
    question, answer = row[0], row[1]

    class Draft(BaseModel):
        category: str = Field(..., description="用例维度：routing/sql/fact/safety/rag 之一")
        expected_agents: list[str] = Field(default_factory=list, description="期望调度 Agent key")
        expected_sql_pattern: str = Field("", description="期望 SQL 模式或 rag:<部门>；- 不判")
        expected_answer_key: str = Field("", description="期望答案关键词（、分隔）；JUDGE: 前缀交 LLM 裁判")

    system = (
        "你是评估用例设计专家。根据一条真实用户点踩的问答，生成回归用例的期望值草稿。\n"
        "约束：\n"
        "1. 期望答案关键词必须来自正确事实——如果原回答可能是错的，用 JUDGE: 前缀交给 LLM 裁判，"
        "不要把可能有误的回答关键词固化为期望；\n"
        "2. expected_agents 用系统能力域：operation/finance/logistics/product/decision；\n"
        "3. expected_sql_pattern 填数据表名或用 rag:<部门>；不确定填 '-'\n"
        "4. category 按问题性质选 routing/sql/fact/safety/rag。"
    )
    messages = [
        SystemMessage(content=system),
        HumanMessage(content=f"用户问题：{question}\n\n被点踩的回答：{answer or '(空)'}"),
    ]
    draft = invoke_structured(get_chat_model(tier="small"), Draft, messages,
                              logger_name="scheduler.harvest")
    if draft is None:
        return {"ok": False, "reason": "llm_unavailable"}
    return {"ok": True, "draft": draft}


def promote(harvest_id: int, reviewer_user_id: str, expected: dict[str, Any]) -> dict[str, Any]:
    """开发人员确认后转正：插入 evaluation_cases（category='online'）并标记 harvest。"""
    rev_id = resolve_user_id(reviewer_user_id)
    expected_agents = expected.get("expected_agents") or []
    sql_pattern = str(expected.get("expected_sql_pattern") or "-")
    answer_key = str(expected.get("expected_answer_key") or "")
    category = str(expected.get("category") or "online")

    with connect() as conn:
        row = conn.execute(
            "SELECT id, question, status FROM evaluation_harvest WHERE id=%s", (harvest_id,)
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if row[2] != "pending":
            return {"ok": False, "reason": f"not_pending:{row[2]}"}

        case_id = conn.execute(
            "INSERT INTO evaluation_cases (question, expected_agents, expected_sql_pattern, "
            "expected_answer_key, category) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (row[1], Jsonb({"required": expected_agents}), sql_pattern, answer_key, category),
        ).fetchone()[0]
        conn.execute(
            "UPDATE evaluation_harvest SET status='promoted', promoted_case_id=%s, "
            "updated_at=now() WHERE id=%s",
            (case_id, harvest_id),
        )
    logger.info("harvest.promoted", harvest_id=harvest_id, case_id=case_id,
                reviewer=reviewer_user_id, category=category)
    return {"ok": True, "case_id": case_id}


def discard(harvest_id: int, reviewer_user_id: str) -> dict[str, Any]:
    """弃用采集条目（标记 discarded，保留审计）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT id, status FROM evaluation_harvest WHERE id=%s", (harvest_id,)
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if row[1] != "pending":
            return {"ok": False, "reason": f"not_pending:{row[1]}"}
        conn.execute("UPDATE evaluation_harvest SET status='discarded', updated_at=now() WHERE id=%s",
                     (harvest_id,))
    logger.info("harvest.discarded", harvest_id=harvest_id, reviewer=reviewer_user_id)
    return {"ok": True}
