# -*- coding: utf-8 -*-
"""重灌 knowledge_chunks / knowledge_embeddings 为模拟向量（确定性哈希伪向量）。

原种子向量为随机值（相似度检索无意义）；用 mock_embedding 重算后，
文本重叠度高的 chunk 向量接近，检索可用、可复现。将来接真实 embedding 模型时重跑本脚本即可。
"""
import sys
sys.path.insert(0, ".")

from app.memory.db import connect
from app.memory.embeddings import mock_embedding, vector_from_sql, vector_to_sql
from app.config.settings import settings

with connect() as conn:
    rows = conn.execute("SELECT id, content FROM knowledge_chunks ORDER BY id").fetchall()
    n = 0
    for cid, content in rows:
        vec = mock_embedding(content or "")
        conn.execute(
            "UPDATE knowledge_chunks SET embedding = %s WHERE id = %s",
            (vector_to_sql(vec), cid),
        )
        # knowledge_embeddings 同步（model=seed_random 模拟模型；无唯一约束，先删后插）
        conn.execute("DELETE FROM knowledge_embeddings WHERE chunk_id = %s", (cid,))
        conn.execute(
            "INSERT INTO knowledge_embeddings (chunk_id, model, dimension, embedding) VALUES (%s, 'seed_random_mock', %s, %s)",
            (cid, len(vec), vector_to_sql(vec)),
        )
        n += 1
    # 校验
    check = conn.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE embedding IS NOT NULL").fetchone()[0]
    print(f"重灌完成: {n} 个 chunk，已有 embedding 的 chunk: {check}")
