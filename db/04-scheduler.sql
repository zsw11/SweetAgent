-- ============================================================
-- 04-scheduler.sql 定时任务调度（NL2Cron，2026-10-02）
-- 对应开发日志考点六十四：能力域模板 + 三档置信兜底 + 审核
-- 工程要求：参数Schema校验 / 权限 / 风险分级 / 审核流 /
--           审计 / 限流 / 幂等 / 重试 / 防雪崩
-- 执行用户：app_user（表属主）；执行库：sweetnight_agent
-- ============================================================

SET ROLE app_user;
SET search_path TO public;

-- ============================================================
-- 定时任务表（唯一权威源：重启时从本表重放注册进 APScheduler）
-- 状态机：pending(待审核) → approved(已通过) / rejected(已拒绝)
--         approved → active(调度中) / paused(暂停) / disabled(禁用)
-- 任何变更写 scheduler_job_events 审计（见下）
-- ============================================================
select * from scheduler_run_logs;
select * from scheduler_jobs;
CREATE TABLE IF NOT EXISTS scheduler_jobs (
    id                BIGSERIAL PRIMARY KEY,               -- 任务ID
    user_id           BIGINT NOT NULL REFERENCES users(id),-- 创建者（权限归属）
    name              VARCHAR(200) NOT NULL,               -- 任务名（用户可读）
    capability_domain VARCHAR(50)  NOT NULL,               -- 能力域（registry 注册的 domain key）
    params            JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 业务参数（须过能力域 Schema 校验）
    time_expr         VARCHAR(200) NOT NULL,               -- 用户原始自然语言时间表达（审计可回溯）
    cron_expr         VARCHAR(100) NOT NULL,               -- 解析后的 cron（时间解析器产出）
    timezone          VARCHAR(50)  NOT NULL DEFAULT 'Asia/Shanghai',  -- 时区
    risk_level        VARCHAR(10)  NOT NULL DEFAULT 'low', -- 风险分级（low/mid/high）
    status            VARCHAR(20)  NOT NULL DEFAULT 'pending', -- 状态机：pending/approved/rejected/active/paused/disabled
    review_note       TEXT,                                -- 审核意见（通过/拒绝理由）
    reviewed_by       BIGINT REFERENCES users(id),         -- 审核人（开发人员）
    reviewed_at       TIMESTAMPTZ,                         -- 审核时间
    last_run_at       TIMESTAMPTZ,                         -- 上次实际触发时间（幂等校验用）
    last_run_status   VARCHAR(20),                         -- 上次执行结果（success/failed/retried）
    consecutive_fail  INT NOT NULL DEFAULT 0,              -- 连续失败次数（≥阈值自动暂停）
    run_count         BIGINT NOT NULL DEFAULT 0,           -- 累计触发次数
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,       -- 调度器是否启用（False=不注册）
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_status ON scheduler_jobs (status);
CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_user ON scheduler_jobs (user_id);
CREATE INDEX IF NOT EXISTS idx_scheduler_jobs_domain ON scheduler_jobs (capability_domain);

-- 幂等约束：同一用户下同名同能力域同 cron 不重复创建
CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduler_jobs_identity
    ON scheduler_jobs (user_id, name, capability_domain, cron_expr);

-- ============================================================
-- 执行记录 / 审计日志（每次触发 + 每次状态变更都写一条）
-- 防止重复触发（幂等）、追溯滥用（审计）、雪崩定位（重试/失败）
-- ============================================================
CREATE TABLE IF NOT EXISTS scheduler_run_logs (
    id          BIGSERIAL PRIMARY KEY,               -- 日志ID
    job_id      BIGINT NOT NULL REFERENCES scheduler_jobs(id) ON DELETE CASCADE,  -- 任务ID
    event_type  VARCHAR(20) NOT NULL,                -- created/approved/rejected/exec_start/exec_success/exec_failed/retry/paused/disabled/deleted/duplicate_skipped
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- 事件详情（参数快照/错误信息/重试次数/限流原因等）
    run_at      TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 事件时间
    duration_ms INT,                                 -- 执行耗时（exec_* 事件）
    result      TEXT                                 -- 执行结果摘要（截断）
);
CREATE INDEX IF NOT EXISTS idx_scheduler_run_logs_job ON scheduler_run_logs (job_id, run_at DESC);
CREATE INDEX IF NOT EXISTS idx_scheduler_run_logs_type ON scheduler_run_logs (event_type, run_at DESC);

-- ============================================================
-- 限流/幂等辅助表：任务级执行锁（同一任务同一时刻只允许一个执行实例）
-- 用 SELECT ... FOR UPDATE 或唯一键实现"重复触发跳过"
-- ============================================================
CREATE TABLE IF NOT EXISTS scheduler_locks (
    job_id      BIGINT PRIMARY KEY REFERENCES scheduler_jobs(id) ON DELETE CASCADE,  -- 任务ID
    locked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 加锁时间
    lock_token  VARCHAR(64) NOT NULL,                -- 锁令牌（执行实例唯一标识）
    expires_at  TIMESTAMPTZ NOT NULL                 -- 锁过期时间（防死锁）
);

-- ============================================================
-- 失败案例采集池（用户点踩回流，2026-10-02，开发日志考点六十六）
-- 设计：quality_gate 防的是"机器认为不行"，用户点踩是"用户认为不行"，
--       后者才是评估体系该回归的信号。只采用户点踩，不做其他自动采集。
-- 流转：user_downvote → 开发人员筛选 → 半自动转正（LLM 预填 expected_* 草稿
--       + 人工确认）→ 插入 evaluation_cases（category='online'）
--       → eval_regression 的 load_cases 自动带上（评估逻辑零改动）
-- ============================================================
select * from  evaluation_harvest;
CREATE TABLE IF NOT EXISTS evaluation_harvest (
    id            BIGSERIAL PRIMARY KEY,              -- 采集ID
    user_id       BIGINT NOT NULL REFERENCES users(id), -- 点踩用户
    thread_id     VARCHAR(100) NOT NULL,              -- 会话ID
    question      TEXT NOT NULL,                      -- 原始问题（脱敏后）
    answer        TEXT,                               -- 回答快照（脱敏后）
    run_id        VARCHAR(64),                        -- LangSmith run id（trace 可回溯）
    trace_url     TEXT,                               -- trace 链接
    comment       TEXT,                               -- 用户点踩时填的"哪里不对"
    signal_type   VARCHAR(30) NOT NULL DEFAULT 'user_downvote',  -- 信号类型（当前仅 user_downvote）
    status        VARCHAR(20) NOT NULL DEFAULT 'pending',        -- pending/promoted/discarded
    promoted_case_id BIGINT,                          -- 转正后的 evaluation_cases.id
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(), -- 采集时间
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()  -- 更新时间
);
CREATE INDEX IF NOT EXISTS idx_harvest_status ON evaluation_harvest (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_harvest_user ON evaluation_harvest (user_id);

-- 幂等去重：同一用户同一问题不重复采集（防刷屏）
CREATE UNIQUE INDEX IF NOT EXISTS uq_harvest_dedup
    ON evaluation_harvest (user_id, question, signal_type);
