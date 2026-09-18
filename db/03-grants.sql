-- ============================================================
-- 03-grants.sql Agent 只读授权（超级用户执行，幂等）
-- 对应设计文档 18.1 节
-- ============================================================

-- ① 允许进入 public schema（访问其中表的前提；不是写权限）
GRANT USAGE ON SCHEMA public TO agent_reader;

-- ② 已存在表：仅 SELECT
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_reader;

-- ③ 未来新建表：默认仅 SELECT（两个实际建表来源都要覆盖）
--    - 当前执行者（langgraph_user / docker 初始化流程）
--    - app_user（应用 / alembic 迁移实际建表方）
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agent_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE app_user IN SCHEMA public GRANT SELECT ON TABLES TO agent_reader;

-- ④ 只读兜底：撤销已存在表的一切写权限（即使未来被误授写权限也能拦截）
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA public FROM agent_reader;

-- ⑤ 序列安全：显式撤销序列上的所有权限，阻止 nextval/currval 滥用
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM agent_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE app_user IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM agent_reader;
