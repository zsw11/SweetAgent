"""知识库摄入：文档 → 切分 → 向量化 → 写入 knowledge_documents / chunks / embeddings。

幂等设计：
- 以 (title) 为唯一键，重灌时先 DELETE 旧文档（ON DELETE CASCADE 自动清掉
  chunks / embeddings），再重建——重复执行结果一致，适合种子脚本反复运行。
- 每个 chunk 的向量写入 knowledge_embeddings（model / dimension / embedding），
  与 chunks 表解耦：同一文本可存多模型向量，模型升级可对比、可追溯。

数据流（设计文档 36-38 节）：
    content(sections) → chunker.chunk_text → embedder.embed_batch
        → knowledge_documents (1 行) + knowledge_chunks (N 行) + knowledge_embeddings (N 行)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from app.knowledge.chunker import build_document_content, chunk_text
from app.knowledge.embedder import (
    current_embedding_model,
    current_vector_dim,
    embed_batch,
)
from app.memory.embeddings import vector_to_sql
from app.observability.logging import get_logger

logger = get_logger("knowledge_ingest")


def _content_hash(content: str) -> str:
    """全文 SHA-256 内容指纹：用于变更检测（同 title 同哈希 = 内容未变，跳过重建）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def ingest_document(
    conn,
    *,
    title: str,
    source_type: str,
    department: str,
    brand: Optional[str] = None,
    market: Optional[str] = None,
    version: Optional[str] = None,
    content: Optional[str] = None,
    sections: Optional[dict[str, str]] = None,
    status: str = "active",
) -> dict[str, Any]:
    """摄入一篇文档（幂等：同 title 先删后建）。

    Args:
        conn: 写连接（app_user，表属主）。
        content: 文档全文（段落用空行分隔）；与 sections 二选一。
        sections: {小节标题: 正文}，自动拼成结构化文本（推荐，便于段落切分）。

    Returns:
        {"document_id", "chunk_count", "model", "dimension"}
    """
    if sections:
        content = build_document_content(sections)
    content = (content or "").strip()
    if not content:
        raise ValueError(f"文档 {title} 内容为空，拒绝摄入（content 与 sections 至少提供一个）")

    content_hash = _content_hash(content)
    # 变更检测：同 title 且全文 SHA-256 相同 → 内容未变，跳过重建（零成本幂等）。
    # 幂等键当前 = title + content_hash（version 未参与任何逻辑，仅作展示元数据）。
    # 【演进 B · 已记录-还未实现】唯一键改 (title, department, brand, market, version)：后面用version维护版本
    # 同 title 不同版本并存（历史可查），检索 ORDER BY version DESC 取最新；见 development_log 待办 18。
    cur = conn.execute(
        "SELECT id, content_hash FROM knowledge_documents WHERE title = %s", (title,)
    )
    existing = cur.fetchone()
    if existing is not None and existing[1] == content_hash:
        logger.info(
            "knowledge.ingest.skip", title=title, document_id=existing[0],
            reason="content_unchanged", content_hash=content_hash[:12],
        )
        return {
            "document_id": existing[0], "chunk_count": 0,
            "model": current_embedding_model(), "dimension": current_vector_dim(),
            "content_hash": content_hash, "skipped": True,
        }

    # 同 title 但内容不同（或无旧文档）→ 先删后建（CASCADE 清 chunks/embeddings）
    if existing is not None:
        conn.execute("DELETE FROM knowledge_documents WHERE title = %s", (title,))
    cur = conn.execute(
        "INSERT INTO knowledge_documents "
        "(title, source_type, department, brand, market, version, content_hash, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (title, source_type, department, brand, market, version, content_hash, status),
    )
    doc_id = cur.fetchone()[0]

    chunks = chunk_text(content)
    vectors = embed_batch(chunks)
    model = current_embedding_model()
    dim = current_vector_dim()

    for i, (chunk_text_i, vec) in enumerate(zip(chunks, vectors)):
        meta = {"department": department, "document_type": source_type}
        cur = conn.execute(
            "INSERT INTO knowledge_chunks "
            "(document_id, chunk_index, content, metadata, embedding) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (doc_id, i, chunk_text_i, json.dumps(meta, ensure_ascii=False), vector_to_sql(vec)),
        )
        chunk_id = cur.fetchone()[0]
        conn.execute(
            "INSERT INTO knowledge_embeddings (chunk_id, model, dimension, embedding) "
            "VALUES (%s, %s, %s, %s)",
            (chunk_id, model, dim, vector_to_sql(vec)),
        )

    logger.info(
        "knowledge.ingest.document",
        document_id=doc_id, title=title, department=department,
        chunks=len(chunks), model=model, dim=dim,
    )
    return {
        "document_id": doc_id, "chunk_count": len(chunks),
        "model": model, "dimension": dim,
        "content_hash": content_hash, "skipped": False,
    }


def ingest_many(conn, docs: list[dict[str, Any]]) -> dict[str, int]:
    """批量摄入多篇文档（每篇一个 ingest_document 的参数 dict）。

    Returns:
        {"ingested": 实际重建/新入库的篇数, "skipped": 内容未变跳过的篇数}
    """
    ok = skipped = 0
    for doc in docs:
        result = ingest_document(conn, **doc)
        if result.get("skipped"):
            skipped += 1
        else:
            ok += 1
    return {"ingested": ok, "skipped": skipped}
