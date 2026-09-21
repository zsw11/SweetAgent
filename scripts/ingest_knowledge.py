# -*- coding: utf-8 -*-
"""企业知识库摄入脚本：把 app/knowledge/seed_docs.py 的种子文档幂等灌入数据库。

幂等：同 title 先删后建（CASCADE 清 chunks/embeddings），可重复执行。
无真实 embedding Key 时使用模拟向量（同文本=同向量），有 Key 时自动用真实模型。

用法：
    .venv\\Scripts\\python scripts\\ingest_knowledge.py            # 摄入全部种子文档
    .venv\\Scripts\\python scripts\\ingest_knowledge.py --check    # 只打印库内文档/chunk 统计
"""
import sys
sys.path.insert(0, ".")

from app.config.settings import settings
from app.knowledge.embedder import current_embedding_model, current_vector_dim
from app.knowledge.ingest import ingest_many
from app.knowledge.seed_docs import KNOWLEDGE_DOCS
from app.memory.db import connect
from app.observability.logging import get_logger

logger = get_logger("ingest_knowledge")


def _check(conn=None) -> None:
    """打印知识库统计；conn 为空则自建连接（独立事务），否则用传入连接（同事务可见）。"""
    owns = conn is None
    if owns:
        conn = connect()
    try:
        docs = conn.execute(
            "SELECT department, COUNT(*) FROM knowledge_documents WHERE status='active' GROUP BY 1 ORDER BY 1"
        ).fetchall()
        total_chunks = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        total_emb = conn.execute("SELECT COUNT(*) FROM knowledge_embeddings").fetchone()[0]
    finally:
        if owns:
            conn.close()
    print("知识库文档统计（按部门）:")
    for dept, cnt in docs:
        print(f"  {dept}: {cnt} 篇")
    print(f"chunks 总数: {total_chunks} | embeddings 总数: {total_emb}")


def main() -> None:
    if "--check" in sys.argv:
        _check()
        return
    print(f"向量模型: {current_embedding_model()} (dim={current_vector_dim()})")
    with connect() as conn:
        result = ingest_many(conn, KNOWLEDGE_DOCS)
        print(f"摄入完成: 重建 {result['ingested']} 篇 / 内容未变跳过 {result['skipped']} 篇")
        _check(conn)


if __name__ == "__main__":
    main()
