-- ============================================================
-- 01-extensions.sql 数据库扩展（超级用户执行，幂等）
-- 执行库：sweetnight_agent
-- ============================================================

-- pgvector：知识库向量检索（design doc 35 节）
CREATE EXTENSION IF NOT EXISTS vector;

-- pg_trgm：文本模糊检索（可选）
CREATE EXTENSION IF NOT EXISTS pg_trgm;
