"""语义长期记忆（设计文档 35 节）：user_memories 表。

存：用户私有、自由文本形态的沉淀（偏好语义 / 事实 / 历史结论 / 业务规则），
department 为独立列（operation/finance/logistics/product；空=通用记忆，不参与粗筛淘汰），
memory_type 区分约束强度：preference/rule（用户要求/业务规则，强）vs fact/conclusion（事实/历史结论，弱）。

写入策略（diff 式）：
- 与已有记忆相似度 > 0.7（max(mock 余弦, n-gram Jaccard)）→ 视为同主题，更新旧条（覆盖 + updated_at），不新增
- 否则新增一条；superseded_at 预留（冲突取代 / 手动删除时软删）

检索策略（两级过滤）：
1. 部门粗筛：department ∈ 问题意图部门 ∪ 无标签通用记忆
2. 向量 top-k：按相似度排序，受 token 预算约束
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.memory.db import connect, resolve_user_id
from app.memory.embeddings import (
    cosine_similarity,
    mock_embedding,
    text_similarity,
    vector_from_sql,
    vector_to_sql,
)
from app.observability.logging import get_logger

logger = get_logger("memory_semantic")

_SIMILARITY_DEDUP = 0.7  # 同主题判定阈值（综合 max(余弦, n-gram Jaccard)，高于此值视为重复/更新而非新增）


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def add_memory(
    user_id: str,
    memory_type: str,
    content: str,
    department: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    confidence: Optional[float] = None,
    evidence: Optional[str] = None,
) -> int:
    """写入一条非结构化记忆（diff 式：同主题更新旧条，否则新增）。

    department 写独立列（空=通用记忆）；metadata 只存扩展标签（topic 等）。
    返回记忆 id。
    """
    uid = resolve_user_id(user_id)
    content = (content or "").strip()
    if not content:
        return -1
    vec = mock_embedding(content)
    meta = dict(metadata or {})  # 扩展标签；department 不再放这里

    with connect() as conn:
        # 1) 同主题检测：取该用户最近 50 条未取代记忆，算文本相似度
        rows = conn.execute(
            """
            SELECT id, content FROM user_memories
            WHERE user_id = %s AND superseded_at IS NULL
            ORDER BY updated_at DESC LIMIT 50
            """,
            (uid,),
        ).fetchall()
        best_id, best_sim = None, 0.0
        for mid, old_content in rows:
            sim = text_similarity(content, old_content)
            if sim > best_sim:
                best_id, best_sim = mid, sim

        if best_id is not None and best_sim >= _SIMILARITY_DEDUP:
            # 同主题：更新旧条（latest-wins 覆盖内容/标签/置信度）
            conn.execute(
                """
                UPDATE user_memories
                SET content = %s, department = %s, metadata = %s, confidence = %s, evidence = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (content, department, json.dumps(meta, ensure_ascii=False), confidence, evidence, best_id),
            )
            logger.info("memory.updated", user_id=user_id, memory_id=best_id, sim=round(best_sim, 3), mtype=memory_type)
            return best_id

        # 2) 新增
        cur = conn.execute(
            """
            INSERT INTO user_memories
                (user_id, memory_type, content, department, metadata, confidence, evidence, embedding, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now())
            RETURNING id
            """,
            (uid, memory_type, content, department, json.dumps(meta, ensure_ascii=False),
             confidence, evidence, vector_to_sql(vec)),
        )
        mid = cur.fetchone()[0]
        logger.info("memory.added", user_id=user_id, memory_id=mid, mtype=memory_type, department=department)
        return mid


# ---------------------------------------------------------------------------
# 检索（两级过滤：部门粗筛 + 向量 top-k）
# ---------------------------------------------------------------------------

def search_memories(
    user_id: str,
    query_text: str,
    departments: Optional[list[str]] = None,
    top_k: int = 5,
    memory_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    """检索用户非结构化记忆。

    departments: 问题意图部门列表；粗筛规则 = department ∈ departments ∪ 无标签通用记忆。
                 为 None 时跳过粗筛（全局检索）。
    """
    uid = resolve_user_id(user_id)
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
            "SELECT id, memory_type, content, department, confidence, evidence, created_at, updated_at "
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
