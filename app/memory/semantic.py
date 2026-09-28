"""语义长期记忆（设计文档 35 节）：user_memories 表。

存：用户私有、自由文本形态的沉淀（偏好语义 / 事实 / 历史结论 / 业务规则），
department 为独立列（operation/finance/logistics/product；空=通用记忆，不参与粗筛淘汰），
memory_type 区分约束强度：preference/rule（用户要求/业务规则，强）vs fact/conclusion（事实/历史结论，弱）。

写入策略（两阶段，参考 Mem0 / LangMem 的 ADD/UPDATE/NONE 决策模式，2026-09-20 重构）：
1. 召回：相似度只负责召回候选（top-5），不负责决策
2. 裁判：最高相似度 > 0.5 才调 LLM（judge.py）判断语义关系 →
   - unrelated → ADD（新增）
   - duplicate → NONE（只刷新旧条 evidence/confidence，不新增）
   - supplement → MERGE（旧条 superseded + 插入合并后新条）
   - conflict/negation → UPDATE（新置信度足够则旧条 superseded + 插入新条）或 NONE
3. 版本化：UPDATE/MERGE 不原地覆盖——旧条 superseded_at=now()、metadata.superseded_by_id=新id、
   新条插入（版本链 m1→m2→m3，历史可追溯）；决策痕迹记 metadata（decision_reason/decided_by）
4. 降级：LLM 不可用/解析失败 → 最高相似度 ≥ 0.7 版本化更新，否则新增

检索策略（两级过滤）：
1. 部门粗筛：department ∈ 问题意图部门 ∪ 无标签通用记忆
2. 向量 top-k：按相似度排序，受 token 预算约束
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.config.settings import settings
from app.memory.db import connect, resolve_user_id
from app.memory.embeddings import (
    cosine_similarity,
    mock_embedding,
    text_similarity,
    vector_from_sql,
    vector_to_sql,
)
from app.memory.judge import judge_memory
from app.observability.logging import get_logger

logger = get_logger("memory_semantic")

_RECALL_THRESHOLD = 0.25  # 召回门槛：最高相似度高于此值才调 LLM 裁判（成本控制，明显无关不问）。
# 0.25 依据：语义变更"用户负责美国市场运营"→"不再负责…转负责欧洲市场" sim≈0.30 需进裁判；完全无关 sim≈0 跳过
_FALLBACK_THRESHOLD = 0.7  # 降级阈值（无 LLM）：最高相似度高于此值版本化更新，否则新增
_CORRECTION_THRESHOLD = 0.5  # 块C 用户显式纠错匹配阈值：纠错内容常反转（下滑→回升），低于常规更新 0.7
_RECALL_TOP_K = 5          # 召回候选数（一次 LLM 调用处理全部候选）


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

# 时效遗忘（块B）：弱记忆（fact/conclusion）超 TTL 惰性软过期；强约束（preference/rule）常驻
_TTL_WEAK_TYPES = ("fact", "conclusion")


def expire_stale_memories(user_id: str) -> int:
    """惰性软过期：弱记忆超过 MEMORY_WEAK_TTL_DAYS 未更新 → superseded_at=now()（软删除，物理行保留可追溯）。

    在 add_memory / search_memories 入口触发，零后台任务；强约束（preference/rule）不参与。
    Returns: 本次过期的条数（幂等，重复调用返回 0）。
    """
    uid = resolve_user_id(user_id)
    ttl_days = int(getattr(settings, "MEMORY_WEAK_TTL_DAYS", 90))
    with connect() as conn:
        cur = conn.execute(
            "UPDATE user_memories SET superseded_at = now() "
            "WHERE user_id = %s AND superseded_at IS NULL "
            f"AND memory_type IN ({','.join(['%s'] * len(_TTL_WEAK_TYPES))}) "
            "AND updated_at < now() - make_interval(days => %s)",
            (uid, *_TTL_WEAK_TYPES, ttl_days),
        )
        conn.commit()
        n = cur.rowcount
    if n:
        logger.info("memory.expire.stale", user_id=uid, count=n, ttl_days=ttl_days)
    return n


def add_memory(
    user_id: str,
    memory_type: str,
    content: str,
    department: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    confidence: Optional[float] = None,
    evidence: Optional[str] = None,
    user_correction: bool = False,
    correction_target: Optional[str] = None,
) -> int:
    """写入一条非结构化记忆（召回 → LLM 裁判 → 版本化执行）。

    department 写独立列（空=通用记忆）；metadata 存扩展标签 + 决策痕迹。
    返回记忆 id（新增返回新 id；duplicate 返回被刷新的旧条 id；UPDATE/MERGE 返回新条 id）。

    user_correction（块C·M档，用户显式纠错）：True 时"以用户为准"——跳过 LLM 裁判，
        1) 优先用 correction_target（被纠对象原话，相似度天然高）召回定位旧条 → 版本化取代；
        2) target 未命中 → 回退 content 相似度匹配（阈值 _CORRECTION_THRESHOLD）；
        3) 仍不命中 → 以用户纠正内容新增（旧条未取代属残余风险，记日志）。
        metadata 记 source=user_correction / decided_by=user / correction_target，供审计。
    correction_target：被纠正旧内容的原话要点（由提示词判断层的 LLM 输出），用于精准定位旧记忆。
    """
    uid = resolve_user_id(user_id)
    content = (content or "").strip()
    if not content:
        return -1
    # 块B：写入前惰性软过期弱记忆，避免与已过期记忆比较/召回
    expire_stale_memories(user_id)
    vec = mock_embedding(content)
    meta = dict(metadata or {})

    with connect() as conn:
        # 1) 召回：未取代记忆按相似度排序取 top-5（相似度只用于召回）
        top, best_sim = _recall(conn, uid, vec)

        # 块C：用户显式纠错（M档）——高置信信号，跳过 LLM 裁判，以用户为准：
        if user_correction:
            meta["source"] = "user_correction"
            meta["decided_by"] = "user"
            # a) 优先用 target（被纠对象原话）定位旧条：target 即旧内容本身，相似度天然高，命中准
            if correction_target and correction_target.strip():
                meta["correction_target"] = correction_target.strip()[:200]
                t_top, t_best = _recall(conn, uid, mock_embedding(correction_target.strip()))
                if t_best >= _CORRECTION_THRESHOLD:
                    logger.info(
                        "memory.correction.target_hit", user_id=uid,
                        target_sim=round(t_best, 3), old_id=t_top[0][1][0],
                    )
                    return _versioned_replace(conn, uid, t_top[0][1], memory_type, content,
                                              department, meta, confidence, evidence, vec)
            # b) target 未命中 → 回退 content 相似度匹配（纠错内容与旧条主体相同仍应命中）
            if best_sim >= _CORRECTION_THRESHOLD:
                logger.info(
                    "memory.correction.content_hit", user_id=uid,
                    sim=round(best_sim, 3), old_id=top[0][1][0],
                )
                return _versioned_replace(conn, uid, top[0][1], memory_type, content,
                                          department, meta, confidence, evidence, vec)
            # c) 均未命中 → 以用户纠正内容新增（旧条未被取代属残余风险，由 TTL/后续整理兜底）
            logger.info("memory.correction.no_match_add", user_id=uid, best_sim=round(best_sim, 3))
            return _insert(conn, uid, memory_type, content, department, meta, confidence, evidence, vec)

        # 2) 门槛：明显无关 → 直接新增（零 LLM 成本）
        if best_sim <= _RECALL_THRESHOLD:
            return _insert(conn, uid, memory_type, content, department, meta, confidence, evidence, vec)

        # 3) 规则前置：内容完全相同 → 直接 NONE 刷新（确定性，不调 LLM；LLM 对多候选可能不稳定）
        candidates = [
            {"id": r[0], "memory_type": r[1], "content": r[2], "confidence": r[3]}
            for _, r in top
        ]
        exact = [cand for cand in candidates if cand["content"] == content]
        if exact:
            meta["decision_reason"] = "exact_duplicate"
            meta["decided_by"] = "rule"
            return _refresh_old(conn, uid, exact[0]["id"], meta, confidence, evidence)

        # 4) LLM 裁判
        decision = judge_memory(content, memory_type, candidates, new_confidence=confidence)

        # 5) 降级：LLM 不可用/解析失败 → 简单阈值版本化
        if decision is None:
            logger.info(
                "memory.judge.fallback", user_id=uid, best_sim=round(best_sim, 3),
                mtype=memory_type, reason="llm_unavailable_or_parse_fail",
            )
            # 降级用综合文本相似度（余弦偏低时 Jaccard 兜底，如"包含关系"场景）
            fallback_sim = text_similarity(content, top[0][1][2]) if top else 0.0
            if fallback_sim >= _FALLBACK_THRESHOLD:
                return _versioned_replace(conn, uid, top[0][1], memory_type, content, department, meta, confidence, evidence, vec)
            return _insert(conn, uid, memory_type, content, department, meta, confidence, evidence, vec)

        event = decision.get("event") or "ADD"
        relation = decision.get("relation") or "unrelated"
        reason = decision.get("reason") or ""
        target_id = decision.get("target_id")
        valid_ids = {c["id"] for c in candidates}
        meta["decision_reason"] = reason
        meta["decided_by"] = "llm"

        # 一致性兜底（judge 小模型常见症状）：reason 文字说"重复/同一事实"，
        # event 却给了 UPDATE/MERGE —— 按 reason 降级为 duplicate/NONE，只刷新旧条，不版本化。
        _DUP_HINTS = ("重复", "同一事实", "换说法", "措辞差异", "同一事件", "同一内容")
        if event in ("UPDATE", "MERGE") and any(h in reason for h in _DUP_HINTS) and target_id in valid_ids:
            logger.info(
                "memory.judge.consistency_fix", user_id=uid,
                original_event=event, target_id=target_id, reason=reason,
            )
            event = "NONE"
            relation = "duplicate"

        # ADD / unrelated：新增
        if event == "ADD" or relation == "unrelated":
            return _insert(conn, uid, memory_type, content, department, meta, confidence, evidence, vec)

        # NONE / duplicate：只刷新旧条（不新增、不覆盖内容）
        if event == "NONE":
            if target_id in valid_ids:
                return _refresh_old(conn, uid, target_id, meta, confidence, evidence)
            return _insert(conn, uid, memory_type, content, department, meta, confidence, evidence, vec)

        # UPDATE / MERGE：版本化（旧条 superseded + 插入新条）
        if target_id in valid_ids:
            new_content = (decision.get("new_content") or "").strip() or content
            return _versioned_replace(conn, uid, next(r for _, r in top if r[0] == target_id),
                                      memory_type, new_content, department, meta, confidence, evidence, vec)

        # target_id 非法：保守新增
        logger.warning("memory.judge.bad_target", user_id=uid, target_id=target_id, mtype=memory_type)
        return _insert(conn, uid, memory_type, content, department, meta, confidence, evidence, vec)


def _recall(conn, uid: int, vec: list[float]) -> tuple[list, float]:
    """召回候选：该用户全部未取代记忆（superseded_at IS NULL）按向量相似度降序取 top-5。

    抽成独立函数：块C 纠错用 correction_target 二次召回时复用（content 与 target 各召一回）。
    Returns: (top 候选列表 [(sim, row), ...], 最高相似度)。
    """
    rows = conn.execute(
        """
        SELECT id, memory_type, content, confidence, embedding FROM user_memories
        WHERE user_id = %s AND superseded_at IS NULL
        """,
        (uid,),
    ).fetchall()
    scored = [(cosine_similarity(vec, vector_from_sql(r[4])), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:_RECALL_TOP_K]
    best_sim = top[0][0] if top else 0.0
    return top, best_sim


def _insert(conn, uid: int, mtype: str, content: str, department: Optional[str],
            meta: dict, confidence: Optional[float], evidence: Optional[str], vec: list[float]) -> int:
    """插入新记忆（memory.added）。"""
    cur = conn.execute(
        """
        INSERT INTO user_memories
            (user_id, memory_type, content, department, metadata, confidence, evidence, embedding, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now())
        RETURNING id
        """,
        (uid, mtype, content, department, json.dumps(meta, ensure_ascii=False),
         confidence, evidence, vector_to_sql(vec)),
    )
    mid = cur.fetchone()[0]
    logger.info("memory.added", user_id=uid, memory_id=mid, mtype=mtype, department=department)
    return mid


def _refresh_old(conn, uid: int, mid: int, meta: dict,
                 confidence: Optional[float], evidence: Optional[str]) -> int:
    """duplicate：不新增、不覆盖内容，只刷新旧条 evidence/confidence/决策痕迹。"""
    conn.execute(
        """
        UPDATE user_memories
        SET confidence = COALESCE(%s, confidence),
            evidence = COALESCE(%s, evidence),
            metadata = metadata || %s::jsonb,
            updated_at = now()
        WHERE id = %s
        """,
        (confidence, evidence, json.dumps(meta, ensure_ascii=False), mid),
    )
    logger.info("memory.refreshed", user_id=uid, memory_id=mid, evidence=bool(evidence), confidence=confidence)
    return mid


def _versioned_replace(conn, uid: int, old_row, mtype: str, new_content: str, department: Optional[str],
                       meta: dict, confidence: Optional[float], evidence: Optional[str], vec: list[float]) -> int:
    """UPDATE/MERGE：旧条 superseded（版本链），插入新条。不物理删除、不原地覆盖。"""
    old_id, old_content = old_row[0], old_row[2]
    # 先插新条，拿到新 id 写回旧条的 superseded_by_id
    cur = conn.execute(
        """
        INSERT INTO user_memories
            (user_id, memory_type, content, department, metadata, confidence, evidence, embedding, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now())
        RETURNING id
        """,
        (uid, mtype, new_content, department, json.dumps(meta, ensure_ascii=False),
         confidence, evidence, vector_to_sql(vec)),
    )
    new_id = cur.fetchone()[0]
    conn.execute(
        """
        UPDATE user_memories
        SET superseded_at = now(),
            metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{superseded_by_id}', %s::jsonb)
        WHERE id = %s
        """,
        (str(new_id), old_id),
    )
    logger.info(
        "memory.superseded", user_id=uid, old_id=old_id, new_id=new_id,
        mtype=mtype, old=old_content[:40], new=new_content[:40],
    )
    return new_id


# ---------------------------------------------------------------------------
# 检索（两级过滤：部门粗筛 + 向量 top-k）
# ---------------------------------------------------------------------------

def search_memories(
    user_id: str,
    query_text: str,
    departments: Optional[list[str]] = None,
    top_k: int = 5,
    memory_type: Optional[str] = None,
    exclude_types: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """检索用户非结构化记忆。

    departments: 问题意图部门列表；粗筛规则 = department ∈ departments ∪ 无标签通用记忆。
                 为 None 时跳过粗筛（全局检索）。
    memory_type: 只返回该类型的记忆（单值）。
    exclude_types: 排除这些类型的记忆（注入分级用：弱记忆检索时排除 rule/preference）。
    """
    uid = resolve_user_id(user_id)
    # 块B：检索前惰性软过期弱记忆，过期记忆 superseded 后自然不返回
    expire_stale_memories(user_id)
    qvec = mock_embedding(query_text)
    with connect() as conn:
        sql = (
            "SELECT id, memory_type, content, department, confidence, evidence, embedding, updated_at "
            "FROM user_memories WHERE user_id = %s AND superseded_at IS NULL"
        )
        params: list[Any] = [uid]
        if memory_type:
            sql += " AND memory_type = %s"
            params.append(memory_type)
        if exclude_types:
            sql += " AND NOT (memory_type = ANY(%s))"
            params.append(list(exclude_types))
        if departments:
            sql += " AND (department = ANY(%s) OR department IS NULL)"
            params.append(departments)
        rows = conn.execute(sql, params).fetchall()

    scored = [
        (cosine_similarity(qvec, vector_from_sql(r[6])), r)
        for r in rows
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for sim, r in scored[:top_k]:
        out.append({
            "id": r[0],
            "memory_type": r[1],
            "content": r[2],
            "department": r[3],
            "confidence": r[4],
            "evidence": r[5],
            "updated_at": str(r[7]) if r[7] else None,
            "similarity": round(sim, 4),
        })
    return out


def list_memories(user_id: str, memory_type: Optional[str] = None) -> list[dict[str, Any]]:
    """列出用户全部未取代记忆（管理接口用）。"""
    uid = resolve_user_id(user_id)
    with connect() as conn:
        sql = (
            "SELECT id, memory_type, content, department, confidence, evidence, created_at, updated_at, metadata "
            "FROM user_memories WHERE user_id = %s AND superseded_at IS NULL ORDER BY updated_at DESC"
        )
        params: list[Any] = [uid]
        if memory_type:
            sql = sql.replace(" ORDER BY", " AND memory_type = %s ORDER BY")
            params.append(memory_type)
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r[0],
            "memory_type": r[1],
            "content": r[2],
            "department": r[3],
            "confidence": r[4],
            "evidence": r[5],
            "created_at": str(r[6]),
            "updated_at": str(r[7]),
            "metadata": r[8] or {},
        }
        for r in rows
    ]


def delete_memory(memory_id: int) -> bool:
    """软删除记忆（superseded_at=now，保留历史可追溯）。"""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE user_memories SET superseded_at = now() WHERE id = %s AND superseded_at IS NULL",
            (memory_id,),
        )
        return cur.rowcount > 0
