"""知识库：ingest / chunker / embedder / retriever（设计文档 36-38 节）。

RAG 闭环：seed_docs(知识文档) → ingest(切分+向量化+写库) → retriever(向量+关键词检索)
→ 部门 Agent 的 knowledge 数据域注入 prompt。
"""
