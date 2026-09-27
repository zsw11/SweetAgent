"""查询 / RAG 缓存（OPT-07）。

- 通用 TTL 缓存：app.cache.ttl_cache.TTLCache
- SQL 结果缓存：get_sql_cache()（ReadOnlyExecutor 集成）
- RAG 检索缓存：get_rag_cache()（KnowledgeRetriever 集成）
- 监控：cache_stats() → 命中率 / 条目数 / 失效次数
"""
