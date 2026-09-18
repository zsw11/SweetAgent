-- ============================================================
-- 00-roles.sql 角色初始化（幂等，可用超级用户在任何库执行）
-- 对应设计文档 18.1 节：Agent 只读角色
-- ============================================================

-- 应用写角色（ORM / checkpoint / 元数据管理）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN PASSWORD 'app_password';
    END IF;
END $$;

-- Agent 只读角色（SQL Tool 统一使用，权限：仅 SELECT）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_reader') THEN
        CREATE ROLE agent_reader LOGIN PASSWORD 'agent_password';
    END IF;
END $$;
