-- ============================================================
-- 02-schema.sql 甜秘密 Multi-Agent 系统全量表结构
-- 对应设计文档 23-32 节（业务表）+ 32 节（Agent 审计表）
-- + 34-38 节（记忆/知识库）+ 46-47 节（Prompt 版本/评估）
-- 执行用户：app_user（表属主）；扩展需由超级用户先建（见 README）
-- 执行库：sweetnight_agent
-- ============================================================

SET ROLE app_user;
SET search_path TO public;

-- ============================================================
-- 23.1 用户与组织
-- ============================================================

CREATE TABLE IF NOT EXISTS departments (
    id          BIGSERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    code        VARCHAR(50)  NOT NULL UNIQUE,
    description TEXT,
    status      VARCHAR(20)  NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      VARCHAR(100) NOT NULL UNIQUE,
    display_name  VARCHAR(100) NOT NULL,
    department_id BIGINT REFERENCES departments(id),
    email         VARCHAR(200),
    status        VARCHAR(20)  NOT NULL DEFAULT 'active',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_roles (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_name  VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS role_permissions (
    id         BIGSERIAL PRIMARY KEY,
    role_name  VARCHAR(50) NOT NULL,
    permission VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 24. 平台 / 市场 / 店铺
-- ============================================================

CREATE TABLE IF NOT EXISTS platforms (
    id         BIGSERIAL PRIMARY KEY,
    code       VARCHAR(50) NOT NULL UNIQUE,
    name       VARCHAR(100) NOT NULL,
    status     VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS markets (
    id         BIGSERIAL PRIMARY KEY,
    code       VARCHAR(10) NOT NULL UNIQUE,
    name       VARCHAR(100) NOT NULL,
    currency   VARCHAR(10) NOT NULL,
    status     VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS brands (
    id          BIGSERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    status      VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stores (
    id          BIGSERIAL PRIMARY KEY,
    platform_id BIGINT NOT NULL REFERENCES platforms(id),
    brand_id    BIGINT NOT NULL REFERENCES brands(id),
    country_code VARCHAR(10) NOT NULL,
    store_name  VARCHAR(200) NOT NULL,
    store_type  VARCHAR(50) NOT NULL DEFAULT 'marketplace',
    status      VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS store_accounts (
    id                BIGSERIAL PRIMARY KEY,
    store_id          BIGINT NOT NULL REFERENCES stores(id),
    account_name      VARCHAR(200) NOT NULL,
    api_key_encrypted TEXT,          -- 第三方平台 Token 密文，Agent 不持有明文
    status            VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 26. 商品
-- ============================================================

CREATE TABLE IF NOT EXISTS product_categories (
    id         BIGSERIAL PRIMARY KEY,
    parent_id  BIGINT REFERENCES product_categories(id),
    name       VARCHAR(100) NOT NULL,
    code       VARCHAR(50)  NOT NULL UNIQUE,
    status     VARCHAR(20) NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS products (
    id           BIGSERIAL PRIMARY KEY,
    brand_id     BIGINT NOT NULL REFERENCES brands(id),
    category_id  BIGINT REFERENCES product_categories(id),
    product_name VARCHAR(200) NOT NULL,
    product_type VARCHAR(50),
    status       VARCHAR(20) NOT NULL DEFAULT 'active',
    launch_date  DATE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS product_skus (
    id           BIGSERIAL PRIMARY KEY,
    product_id   BIGINT NOT NULL REFERENCES products(id),
    sku_code     VARCHAR(100) NOT NULL UNIQUE,
    country_code VARCHAR(10) NOT NULL,
    sale_price   NUMERIC(12,2) NOT NULL,
    cost         NUMERIC(12,2) NOT NULL,
    weight       NUMERIC(10,2),        -- kg
    status       VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS product_prices (
    id             BIGSERIAL PRIMARY KEY,
    sku_id         BIGINT NOT NULL REFERENCES product_skus(id),
    country_code   VARCHAR(10) NOT NULL,
    currency       VARCHAR(10) NOT NULL,
    price          NUMERIC(12,2) NOT NULL,
    effective_from DATE NOT NULL,
    effective_to   DATE
);

CREATE TABLE IF NOT EXISTS product_costs (
    id             BIGSERIAL PRIMARY KEY,
    sku_id         BIGINT NOT NULL REFERENCES product_skus(id),
    cost_type      VARCHAR(50) NOT NULL DEFAULT 'product',
    amount         NUMERIC(12,2) NOT NULL,
    currency       VARCHAR(10) NOT NULL DEFAULT 'USD',
    effective_from DATE NOT NULL,
    effective_to   DATE
);

CREATE TABLE IF NOT EXISTS product_lifecycle (
    id         BIGSERIAL PRIMARY KEY,
    sku_id     BIGINT NOT NULL REFERENCES product_skus(id),
    stage      VARCHAR(50) NOT NULL,   -- new / growth / mature / decline / eol
    start_date DATE NOT NULL,
    end_date   DATE,
    notes      TEXT
);

CREATE TABLE IF NOT EXISTS product_development_projects (
    id                  BIGSERIAL PRIMARY KEY,
    name                VARCHAR(200) NOT NULL,
    brand_id            BIGINT REFERENCES brands(id),
    status              VARCHAR(20) NOT NULL DEFAULT 'draft',
    stage               VARCHAR(50),
    owner_user_id       BIGINT REFERENCES users(id),
    planned_launch_date DATE,
    actual_launch_date  DATE,
    description         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 27. 销售
-- ============================================================

CREATE TABLE IF NOT EXISTS orders (
    id               BIGSERIAL PRIMARY KEY,
    platform_order_id VARCHAR(100),
    store_id         BIGINT NOT NULL REFERENCES stores(id),
    country_code     VARCHAR(10) NOT NULL,
    order_date       DATE NOT NULL,
    currency         VARCHAR(10) NOT NULL DEFAULT 'USD',
    order_amount     NUMERIC(12,2) NOT NULL,
    refund_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,
    status           VARCHAR(20) NOT NULL DEFAULT 'completed',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_items (
    id           BIGSERIAL PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES orders(id),
    sku_id       BIGINT NOT NULL REFERENCES product_skus(id),
    quantity     INT NOT NULL,
    sale_amount  NUMERIC(12,2) NOT NULL,
    discount     NUMERIC(12,2) NOT NULL DEFAULT 0,
    refund_amount NUMERIC(12,2) NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sales_daily (
    date          DATE NOT NULL,
    country       VARCHAR(10) NOT NULL,
    brand_id      BIGINT REFERENCES brands(id),
    store_id      BIGINT NOT NULL REFERENCES stores(id),
    sku_id        BIGINT NOT NULL REFERENCES product_skus(id),
    orders        INT NOT NULL DEFAULT 0,
    units         INT NOT NULL DEFAULT 0,
    gmv           NUMERIC(14,2) NOT NULL DEFAULT 0,
    refund_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    PRIMARY KEY (date, store_id, sku_id)
);

-- ============================================================
-- 28. 广告
-- ============================================================

CREATE TABLE IF NOT EXISTS ad_campaigns (
    id           BIGSERIAL PRIMARY KEY,
    store_id     BIGINT NOT NULL REFERENCES stores(id),
    platform     VARCHAR(50) NOT NULL,
    campaign_name VARCHAR(200) NOT NULL,
    objective    VARCHAR(50),
    status       VARCHAR(20) NOT NULL DEFAULT 'active',
    start_date   DATE,
    end_date     DATE,
    daily_budget NUMERIC(12,2),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ad_groups (
    id           BIGSERIAL PRIMARY KEY,
    campaign_id  BIGINT NOT NULL REFERENCES ad_campaigns(id),
    ad_group_name VARCHAR(200) NOT NULL,
    status       VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ad_creatives (
    id            BIGSERIAL PRIMARY KEY,
    ad_group_id   BIGINT NOT NULL REFERENCES ad_groups(id),
    creative_name VARCHAR(200) NOT NULL,
    creative_type VARCHAR(50) NOT NULL DEFAULT 'image',
    status        VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ad_performance_daily (
    date         DATE NOT NULL,
    platform     VARCHAR(50) NOT NULL,
    store_id     BIGINT NOT NULL REFERENCES stores(id),
    campaign_id  BIGINT NOT NULL REFERENCES ad_campaigns(id),
    sku_id       BIGINT REFERENCES product_skus(id),
    impressions  BIGINT NOT NULL DEFAULT 0,
    clicks       INT NOT NULL DEFAULT 0,
    spend        NUMERIC(14,2) NOT NULL DEFAULT 0,
    conversions  INT NOT NULL DEFAULT 0,
    revenue      NUMERIC(14,2) NOT NULL DEFAULT 0,
    ctr          NUMERIC(8,4),
    cvr          NUMERIC(8,4),
    cpc          NUMERIC(10,4),
    roas         NUMERIC(10,4),
    PRIMARY KEY (date, campaign_id, sku_id)
);

-- ============================================================
-- 29. 库存 / 物流
-- ============================================================

CREATE TABLE IF NOT EXISTS warehouses (
    id           BIGSERIAL PRIMARY KEY,
    code         VARCHAR(50) NOT NULL UNIQUE,
    name         VARCHAR(200) NOT NULL,
    country_code VARCHAR(10) NOT NULL,
    region       VARCHAR(50),
    status       VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS carriers (
    id           BIGSERIAL PRIMARY KEY,
    code         VARCHAR(50) NOT NULL UNIQUE,
    name         VARCHAR(100) NOT NULL,
    service_level VARCHAR(50),
    status       VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS inventory (
    id            BIGSERIAL PRIMARY KEY,
    warehouse_id  BIGINT NOT NULL REFERENCES warehouses(id),
    sku_id        BIGINT NOT NULL REFERENCES product_skus(id),
    available_qty INT NOT NULL DEFAULT 0,
    reserved_qty  INT NOT NULL DEFAULT 0,
    in_transit_qty INT NOT NULL DEFAULT 0,
    safety_stock  INT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (warehouse_id, sku_id)
);

CREATE TABLE IF NOT EXISTS inventory_daily (
    date           DATE NOT NULL,
    warehouse_id   BIGINT NOT NULL REFERENCES warehouses(id),
    sku_id         BIGINT NOT NULL REFERENCES product_skus(id),
    available_qty  INT NOT NULL DEFAULT 0,
    reserved_qty   INT NOT NULL DEFAULT 0,
    in_transit_qty INT NOT NULL DEFAULT 0,
    safety_stock   INT NOT NULL DEFAULT 0,
    stock_days     NUMERIC(10,2),
    PRIMARY KEY (date, warehouse_id, sku_id)
);

CREATE TABLE IF NOT EXISTS inbound_shipments (
    id           BIGSERIAL PRIMARY KEY,
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id),
    sku_id       BIGINT NOT NULL REFERENCES product_skus(id),
    quantity     INT NOT NULL,
    eta_date     DATE,
    status       VARCHAR(20) NOT NULL DEFAULT 'in_transit',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outbound_shipments (
    id           BIGSERIAL PRIMARY KEY,
    order_id     BIGINT REFERENCES orders(id),
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id),
    sku_id       BIGINT NOT NULL REFERENCES product_skus(id),
    quantity     INT NOT NULL,
    ship_date    DATE,
    status       VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS logistics_orders (
    id               BIGSERIAL PRIMARY KEY,
    order_id         BIGINT REFERENCES orders(id),
    carrier_id       BIGINT NOT NULL REFERENCES carriers(id),
    tracking_number  VARCHAR(100),
    warehouse_id     BIGINT REFERENCES warehouses(id),
    ship_date        DATE,
    delivery_date    DATE,
    status           VARCHAR(20) NOT NULL DEFAULT 'created',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS logistics_cost (
    id          BIGSERIAL PRIMARY KEY,
    order_id    BIGINT REFERENCES orders(id),
    carrier_id  BIGINT REFERENCES carriers(id),
    cost_type   VARCHAR(50) NOT NULL DEFAULT 'shipping',
    amount      NUMERIC(12,2) NOT NULL,
    currency    VARCHAR(10) NOT NULL DEFAULT 'USD',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tracking_events (
    id                 BIGSERIAL PRIMARY KEY,
    logistics_order_id BIGINT NOT NULL REFERENCES logistics_orders(id),
    event_code         VARCHAR(50),
    event_name         VARCHAR(200),
    event_date         TIMESTAMPTZ,
    location           VARCHAR(200),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 30. 财务
-- ============================================================

CREATE TABLE IF NOT EXISTS financial_transactions (
    id               BIGSERIAL PRIMARY KEY,
    store_id         BIGINT NOT NULL REFERENCES stores(id),
    transaction_date DATE NOT NULL,
    transaction_type VARCHAR(50) NOT NULL,  -- revenue / fee / refund / logistics / advertising ...
    amount           NUMERIC(14,2) NOT NULL,
    currency         VARCHAR(10) NOT NULL DEFAULT 'USD',
    fx_rate          NUMERIC(12,6) DEFAULT 1,
    description      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS revenue_daily (
    date      DATE NOT NULL,
    store_id  BIGINT NOT NULL REFERENCES stores(id),
    country   VARCHAR(10) NOT NULL,
    revenue   NUMERIC(14,2) NOT NULL DEFAULT 0,
    currency  VARCHAR(10) NOT NULL DEFAULT 'USD',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date, store_id)
);

CREATE TABLE IF NOT EXISTS cost_daily (
    date      DATE NOT NULL,
    store_id  BIGINT NOT NULL REFERENCES stores(id),
    cost_type VARCHAR(50) NOT NULL,
    amount    NUMERIC(14,2) NOT NULL DEFAULT 0,
    currency  VARCHAR(10) NOT NULL DEFAULT 'USD',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date, store_id, cost_type)
);

CREATE TABLE IF NOT EXISTS profit_daily (
    date               DATE NOT NULL,
    store_id           BIGINT NOT NULL REFERENCES stores(id),
    sku_id             BIGINT NOT NULL REFERENCES product_skus(id),
    revenue            NUMERIC(14,2) NOT NULL DEFAULT 0,
    product_cost       NUMERIC(14,2) NOT NULL DEFAULT 0,
    platform_fee       NUMERIC(14,2) NOT NULL DEFAULT 0,
    advertising_cost   NUMERIC(14,2) NOT NULL DEFAULT 0,
    logistics_cost     NUMERIC(14,2) NOT NULL DEFAULT 0,
    refund_cost        NUMERIC(14,2) NOT NULL DEFAULT 0,
    gross_profit       NUMERIC(14,2) NOT NULL DEFAULT 0,
    contribution_profit NUMERIC(14,2) NOT NULL DEFAULT 0,
    profit_margin      NUMERIC(10,4),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date, store_id, sku_id)
);

CREATE TABLE IF NOT EXISTS platform_fees (
    id         BIGSERIAL PRIMARY KEY,
    order_id   BIGINT REFERENCES orders(id),
    store_id   BIGINT NOT NULL REFERENCES stores(id),
    fee_type   VARCHAR(50) NOT NULL,
    amount     NUMERIC(14,2) NOT NULL,
    currency   VARCHAR(10) NOT NULL DEFAULT 'USD',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS refunds (
    id           BIGSERIAL PRIMARY KEY,
    order_id     BIGINT REFERENCES orders(id),
    sku_id       BIGINT REFERENCES product_skus(id),
    refund_amount NUMERIC(12,2) NOT NULL,
    refund_date  DATE NOT NULL,
    reason       VARCHAR(200),
    status       VARCHAR(20) NOT NULL DEFAULT 'completed',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fx_rates (
    currency_pair VARCHAR(10) NOT NULL,   -- 如 USD/EUR
    rate_date     DATE NOT NULL,
    rate          NUMERIC(12,6) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (currency_pair, rate_date)
);

CREATE TABLE IF NOT EXISTS reconciliation_records (
    id               BIGSERIAL PRIMARY KEY,
    period_start     DATE NOT NULL,
    period_end       DATE NOT NULL,
    store_id         BIGINT NOT NULL REFERENCES stores(id),
    platform_revenue NUMERIC(14,2) NOT NULL DEFAULT 0,
    system_revenue   NUMERIC(14,2) NOT NULL DEFAULT 0,
    difference       NUMERIC(14,2) NOT NULL DEFAULT 0,
    status           VARCHAR(20) NOT NULL DEFAULT 'pending',
    reconciled_at    TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 31. 用户评论 / VOC
-- ============================================================

CREATE TABLE IF NOT EXISTS reviews (
    id                BIGSERIAL PRIMARY KEY,
    platform          VARCHAR(50) NOT NULL,
    sku_id            BIGINT NOT NULL REFERENCES product_skus(id),
    country           VARCHAR(10) NOT NULL,
    rating            INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    review_text       TEXT,
    review_date       DATE NOT NULL,
    verified_purchase BOOLEAN DEFAULT true,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS review_aspects (
    id        BIGSERIAL PRIMARY KEY,
    review_id BIGINT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    aspect    VARCHAR(50) NOT NULL,      -- comfort / delivery / quality / price ...
    sentiment VARCHAR(20) NOT NULL,      -- positive / neutral / negative
    score     NUMERIC(4,2)
);

CREATE TABLE IF NOT EXISTS review_sentiments (
    id             BIGSERIAL PRIMARY KEY,
    review_id      BIGINT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    sentiment_type VARCHAR(50) NOT NULL,
    score          NUMERIC(6,4),
    confidence     NUMERIC(6,4)
);

CREATE TABLE IF NOT EXISTS customer_feedback (
    id         BIGSERIAL PRIMARY KEY,
    source     VARCHAR(50) NOT NULL,
    content    TEXT,
    sentiment  VARCHAR(20),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS return_reasons (
    id           BIGSERIAL PRIMARY KEY,
    order_id     BIGINT REFERENCES orders(id),
    sku_id       BIGINT REFERENCES product_skus(id),
    reason_code  VARCHAR(50),
    reason_detail TEXT,
    return_date  DATE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 32. Agent 审计表
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_runs (
    id           BIGSERIAL PRIMARY KEY,
    thread_id    VARCHAR(100),
    user_id      BIGINT REFERENCES users(id),
    question     TEXT NOT NULL,
    status       VARCHAR(20) NOT NULL DEFAULT 'running',
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    total_tokens INT NOT NULL DEFAULT 0,
    total_cost   NUMERIC(12,6) NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agent_steps (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    agent_name    VARCHAR(50) NOT NULL,
    node_name     VARCHAR(100),
    step_index    INT NOT NULL DEFAULT 0,
    input_summary TEXT,
    output_summary TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        VARCHAR(20) NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS agent_tool_calls (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    agent_name    VARCHAR(50) NOT NULL,
    tool_name     VARCHAR(100) NOT NULL,
    arguments     JSONB,
    result_summary TEXT,
    duration_ms   INT,
    status        VARCHAR(20) NOT NULL DEFAULT 'success',
    error_message TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_errors (
    id           BIGSERIAL PRIMARY KEY,
    run_id       BIGINT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    agent_name   VARCHAR(50),
    node_name    VARCHAR(100),
    error_type   VARCHAR(50),
    error_message TEXT,
    retry_count  INT NOT NULL DEFAULT 0,
    stack_trace  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_interrupts (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    thread_id     VARCHAR(100),
    agent_name    VARCHAR(50),
    interrupt_type VARCHAR(50),
    payload       JSONB,
    status        VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ,
    resolved_by   BIGINT REFERENCES users(id),
    resolution    JSONB
);

CREATE TABLE IF NOT EXISTS agent_results (
    id          BIGSERIAL PRIMARY KEY,
    run_id      BIGINT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    agent_name  VARCHAR(50) NOT NULL,
    result_type VARCHAR(50) NOT NULL DEFAULT 'department_result',
    result      JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 34. 结构化长期记忆
-- ============================================================

CREATE TABLE IF NOT EXISTS user_profiles (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key        VARCHAR(100) NOT NULL,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, key)
);

CREATE TABLE IF NOT EXISTS user_preferences (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key        VARCHAR(100) NOT NULL,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, key)
);

CREATE TABLE IF NOT EXISTS business_preferences (
    id         BIGSERIAL PRIMARY KEY,
    key        VARCHAR(100) NOT NULL,
    value      TEXT,
    scope      VARCHAR(50) NOT NULL DEFAULT 'global',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (key, scope)
);

-- ============================================================
-- 35-38. 知识库（PGVector）
-- ============================================================

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id           BIGSERIAL PRIMARY KEY,
    title        VARCHAR(300) NOT NULL,
    source_type  VARCHAR(50) NOT NULL,   -- SOP / report / product_spec / faq ...
    department   VARCHAR(50) NOT NULL,   -- operation / logistics / finance / product
    brand        VARCHAR(100),
    market       VARCHAR(50),
    version      VARCHAR(50),
    status       VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content     TEXT NOT NULL,
    metadata    JSONB,
    embedding   vector(1536),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_embeddings (
    id         BIGSERIAL PRIMARY KEY,
    chunk_id   BIGINT NOT NULL REFERENCES knowledge_chunks(id) ON DELETE CASCADE,
    model      VARCHAR(100) NOT NULL,
    dimension  INT NOT NULL,
    embedding  vector(1536) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 22. Agent 元数据（口径 / 业务规则）
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_metric_definition (
    id          BIGSERIAL PRIMARY KEY,
    metric_name VARCHAR(100) NOT NULL UNIQUE,
    definition  TEXT,
    formula     TEXT,
    unit        VARCHAR(50),
    department  VARCHAR(50) NOT NULL,
    version     VARCHAR(50) NOT NULL DEFAULT 'v1',
    status      VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_business_rule (
    id          BIGSERIAL PRIMARY KEY,
    rule_name   VARCHAR(200) NOT NULL,
    rule_type   VARCHAR(50) NOT NULL,
    description TEXT,
    department  VARCHAR(50) NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 46. Prompt 版本
-- ============================================================

CREATE TABLE IF NOT EXISTS prompt_versions (
    id           BIGSERIAL PRIMARY KEY,
    agent_name   VARCHAR(50) NOT NULL,
    version      VARCHAR(50) NOT NULL,
    content_hash VARCHAR(64),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active    BOOLEAN NOT NULL DEFAULT false
);

-- ============================================================
-- 47. Evaluation
-- ============================================================

CREATE TABLE IF NOT EXISTS evaluation_cases (
    id                   BIGSERIAL PRIMARY KEY,
    question             TEXT NOT NULL,
    expected_agents      JSONB,
    expected_sql_pattern TEXT,
    expected_answer_key  TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    id          BIGSERIAL PRIMARY KEY,
    run_id      VARCHAR(100),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      VARCHAR(20) NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS evaluation_scores (
    id        BIGSERIAL PRIMARY KEY,
    run_id    BIGINT NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
    case_id   BIGINT NOT NULL REFERENCES evaluation_cases(id),
    metric    VARCHAR(50) NOT NULL,
    score     NUMERIC(8,4),
    detail    JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- mart 层（设计文档 22、30 节）：Agent 优先查询层
-- ============================================================

CREATE TABLE IF NOT EXISTS mart_sales_daily (
    date          DATE NOT NULL,
    country       VARCHAR(10) NOT NULL,
    brand_id      BIGINT REFERENCES brands(id),
    store_id      BIGINT NOT NULL REFERENCES stores(id),
    sku_id        BIGINT NOT NULL REFERENCES product_skus(id),
    orders        INT NOT NULL DEFAULT 0,
    units         INT NOT NULL DEFAULT 0,
    gmv           NUMERIC(14,2) NOT NULL DEFAULT 0,
    refund_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    PRIMARY KEY (date, store_id, sku_id)
);

CREATE TABLE IF NOT EXISTS mart_product_profit_daily (
    date               DATE NOT NULL,
    country            VARCHAR(10) NOT NULL,
    brand_id           BIGINT REFERENCES brands(id),
    store_id           BIGINT NOT NULL REFERENCES stores(id),
    sku_id             BIGINT NOT NULL REFERENCES product_skus(id),
    revenue            NUMERIC(14,2) NOT NULL DEFAULT 0,
    product_cost       NUMERIC(14,2) NOT NULL DEFAULT 0,
    platform_fee       NUMERIC(14,2) NOT NULL DEFAULT 0,
    advertising_cost   NUMERIC(14,2) NOT NULL DEFAULT 0,
    logistics_cost     NUMERIC(14,2) NOT NULL DEFAULT 0,
    refund_cost        NUMERIC(14,2) NOT NULL DEFAULT 0,
    gross_profit       NUMERIC(14,2) NOT NULL DEFAULT 0,
    contribution_profit NUMERIC(14,2) NOT NULL DEFAULT 0,
    profit_margin      NUMERIC(10,4),
    PRIMARY KEY (date, store_id, sku_id)
);

CREATE TABLE IF NOT EXISTS mart_ad_performance_daily (
    date         DATE NOT NULL,
    platform     VARCHAR(50) NOT NULL,
    store_id     BIGINT NOT NULL REFERENCES stores(id),
    campaign_id  BIGINT NOT NULL REFERENCES ad_campaigns(id),
    sku_id       BIGINT REFERENCES product_skus(id),
    impressions  BIGINT NOT NULL DEFAULT 0,
    clicks       INT NOT NULL DEFAULT 0,
    spend        NUMERIC(14,2) NOT NULL DEFAULT 0,
    conversions  INT NOT NULL DEFAULT 0,
    revenue      NUMERIC(14,2) NOT NULL DEFAULT 0,
    ctr          NUMERIC(8,4),
    cvr          NUMERIC(8,4),
    cpc          NUMERIC(10,4),
    roas         NUMERIC(10,4),
    PRIMARY KEY (date, campaign_id, sku_id)
);

CREATE TABLE IF NOT EXISTS mart_inventory_risk (
    date           DATE NOT NULL,
    warehouse_id   BIGINT NOT NULL REFERENCES warehouses(id),
    sku_id         BIGINT NOT NULL REFERENCES product_skus(id),
    available_qty  INT NOT NULL DEFAULT 0,
    in_transit_qty INT NOT NULL DEFAULT 0,
    safety_stock   INT NOT NULL DEFAULT 0,
    stock_days     NUMERIC(10,2),
    forecast_demand INT,
    risk_level     VARCHAR(20),          -- high / medium / low
    risk_reason    TEXT,
    PRIMARY KEY (date, warehouse_id, sku_id)
);

-- ============================================================
-- 索引
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_orders_store_date ON orders(store_id, order_date);
CREATE INDEX IF NOT EXISTS idx_orders_country ON orders(country_code);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_sku ON order_items(sku_id);

CREATE INDEX IF NOT EXISTS idx_sales_daily_date ON sales_daily(date);
CREATE INDEX IF NOT EXISTS idx_sales_daily_sku ON sales_daily(sku_id);
CREATE INDEX IF NOT EXISTS idx_sales_daily_store ON sales_daily(store_id);

CREATE INDEX IF NOT EXISTS idx_ad_perf_date_campaign ON ad_performance_daily(date, campaign_id);
CREATE INDEX IF NOT EXISTS idx_ad_perf_date_sku ON ad_performance_daily(date, sku_id);
CREATE INDEX IF NOT EXISTS idx_ad_perf_store ON ad_performance_daily(store_id);

CREATE INDEX IF NOT EXISTS idx_inventory_daily_date_sku ON inventory_daily(date, sku_id);
CREATE INDEX IF NOT EXISTS idx_logistics_order_order ON logistics_orders(order_id);
CREATE INDEX IF NOT EXISTS idx_tracking_logistics ON tracking_events(logistics_order_id);

CREATE INDEX IF NOT EXISTS idx_reviews_sku_date ON reviews(sku_id, review_date);
CREATE INDEX IF NOT EXISTS idx_reviews_country ON reviews(country);
CREATE INDEX IF NOT EXISTS idx_review_aspects_review ON review_aspects(review_id);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_document ON knowledge_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_kb_docs_dept ON knowledge_documents(department);
CREATE INDEX IF NOT EXISTS idx_kb_docs_status ON knowledge_documents(status);

CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_run ON agent_tool_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_errors_run ON agent_errors(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_interrupts_run ON agent_interrupts(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_results_run ON agent_results(run_id);

CREATE INDEX IF NOT EXISTS idx_mart_sales_date ON mart_sales_daily(date);
CREATE INDEX IF NOT EXISTS idx_mart_sales_sku ON mart_sales_daily(sku_id);
CREATE INDEX IF NOT EXISTS idx_mart_profit_date ON mart_product_profit_daily(date);
CREATE INDEX IF NOT EXISTS idx_mart_profit_sku ON mart_product_profit_daily(sku_id);
CREATE INDEX IF NOT EXISTS idx_mart_ad_date ON mart_ad_performance_daily(date);
CREATE INDEX IF NOT EXISTS idx_mart_inv_date ON mart_inventory_risk(date);

-- ============================================================
-- 表与字段注释（COMMENT ON）
-- 供 Agent / 数据分析师快速理解表结构语义；
-- 幂等可重复执行（COMMENT 覆盖式更新）。
-- ============================================================

-- ---------- 23.1 用户与组织 ----------
COMMENT ON TABLE public.departments IS '部门（Operation/Logistics/Finance/Product 等）';
COMMENT ON TABLE public.users IS '系统用户（可关联部门，Agent 审计用）';
COMMENT ON TABLE public.user_roles IS '用户-角色关联';
COMMENT ON TABLE public.role_permissions IS '角色权限定义';

-- ---------- 24. 平台 / 市场 / 店铺 ----------
COMMENT ON TABLE public.platforms IS '销售平台（Amazon/Walmart/官网等）';
COMMENT ON TABLE public.markets IS '目标市场（US/CA/UK 等，含币种）';
COMMENT ON TABLE public.brands IS '品牌（1=SweetNight 2=Novilla 3=Avenco 4=甜秘密）';
COMMENT ON COLUMN public.brands.name IS '品牌名（Agent 过滤品牌时按此字段匹配）';
COMMENT ON TABLE public.stores IS '店铺（平台 x 品牌 x 国家）';
COMMENT ON COLUMN public.stores.store_type IS '店铺类型（marketplace/self-operated）';
COMMENT ON TABLE public.store_accounts IS '店铺第三方平台账号（Token 仅存密文，Agent 不持有明文）';

-- ---------- 26. 商品 ----------
COMMENT ON TABLE public.product_categories IS '商品类目（支持父子层级）';
COMMENT ON TABLE public.products IS '商品主档（挂品牌/类目）';
COMMENT ON TABLE public.product_skus IS 'SKU 主档（核心表：SKU 编码/国家/售价/成本）';
COMMENT ON COLUMN public.product_skus.sku_code IS 'SKU 编码（如 SN-Q12-US，前缀为品牌缩写，不含品牌全名）';
COMMENT ON COLUMN public.product_skus.country_code IS '销售国家（US/CA/UK）';
COMMENT ON COLUMN public.product_skus.sale_price IS '售价（USD）';
COMMENT ON COLUMN public.product_skus.cost IS '成本（USD）';
COMMENT ON TABLE public.product_prices IS 'SKU 分国家价格历史（带生效区间）';
COMMENT ON TABLE public.product_costs IS 'SKU 分类型成本历史（product/shipping/ad 等）';
COMMENT ON TABLE public.product_lifecycle IS '商品生命周期阶段（new/growth/mature/decline/eol）';
COMMENT ON TABLE public.product_development_projects IS '商品开发项目（含计划/实际上架日期）';

-- ---------- 27. 销售 ----------
COMMENT ON TABLE public.orders IS '订单（平台原始订单汇总）';
COMMENT ON COLUMN public.orders.platform_order_id IS '平台订单号';
COMMENT ON COLUMN public.orders.order_date IS '下单日期';
COMMENT ON COLUMN public.orders.order_amount IS '订单金额（含税）';
COMMENT ON COLUMN public.orders.refund_amount IS '退款金额';
COMMENT ON TABLE public.order_items IS '订单明细（SKU 维度）';
COMMENT ON TABLE public.sales_daily IS '销售日报（明细层：日期x店铺xSKU 的订单/销量/GMV）';
COMMENT ON COLUMN public.sales_daily.date IS '日期（Agent 时间窗口过滤字段）';
COMMENT ON COLUMN public.sales_daily.country IS '国家（US/CA/UK）';
COMMENT ON COLUMN public.sales_daily.brand_id IS '品牌 ID（关联 brands）';
COMMENT ON COLUMN public.sales_daily.gmv IS '成交金额（USD）';
COMMENT ON COLUMN public.sales_daily.units IS '销量（件）';
COMMENT ON COLUMN public.sales_daily.orders IS '订单数';

-- ---------- 28. 广告 ----------
COMMENT ON TABLE public.ad_campaigns IS '广告活动（campaign 层：预算/目标/起止）';
COMMENT ON COLUMN public.ad_campaigns.campaign_name IS '活动名（如 SN US Brand Defense）';
COMMENT ON COLUMN public.ad_campaigns.daily_budget IS '每日预算（USD）';
COMMENT ON TABLE public.ad_groups IS '广告组（campaign 下分组）';
COMMENT ON TABLE public.ad_creatives IS '广告创意（图片/视频等素材）';
COMMENT ON TABLE public.ad_performance_daily IS '广告效果日报（impressions/clicks/spend/conversions/revenue + CTR/CVR/CPC/ROAS）';
COMMENT ON COLUMN public.ad_performance_daily.spend IS '广告花费（USD）';
COMMENT ON COLUMN public.ad_performance_daily.roas IS 'ROAS=revenue/spend';
COMMENT ON COLUMN public.ad_performance_daily.cvr IS '转化率=conversions/clicks';
COMMENT ON COLUMN public.ad_performance_daily.ctr IS '点击率=clicks/impressions';

-- ---------- 29. 库存 / 物流 ----------
COMMENT ON TABLE public.warehouses IS '仓库（分国家/区域）';
COMMENT ON TABLE public.carriers IS '物流承运商';
COMMENT ON TABLE public.inventory IS '实时库存快照（warehouse x SKU）';
COMMENT ON COLUMN public.inventory.available_qty IS '可售库存';
COMMENT ON COLUMN public.inventory.safety_stock IS '安全库存阈值';
COMMENT ON TABLE public.inventory_daily IS '库存日报（含 stock_days 库存可售天数）';
COMMENT ON COLUMN public.inventory_daily.stock_days IS '库存天数（可售天数，低于 12 天为高风险）';
COMMENT ON TABLE public.inbound_shipments IS '入库单（在途补货）';
COMMENT ON TABLE public.outbound_shipments IS '出库单（发货）';
COMMENT ON TABLE public.logistics_orders IS '物流运单（承运商/轨迹）';
COMMENT ON TABLE public.logistics_cost IS '物流成本明细';
COMMENT ON TABLE public.tracking_events IS '物流轨迹事件（揽收/运输/妥投等）';

-- ---------- 30. 财务 ----------
COMMENT ON TABLE public.financial_transactions IS '财务流水（revenue/fee/refund/logistics/advertising）';
COMMENT ON TABLE public.revenue_daily IS '收入日报（店铺维度）';
COMMENT ON TABLE public.cost_daily IS '成本日报（按成本类型）';
COMMENT ON TABLE public.profit_daily IS '利润日报（毛利/贡献利润/利润率）';
COMMENT ON COLUMN public.profit_daily.gross_profit IS '毛利=revenue-product_cost';
COMMENT ON COLUMN public.profit_daily.contribution_profit IS '贡献利润=毛利-平台费-广告-物流-退款';
COMMENT ON COLUMN public.profit_daily.profit_margin IS '利润率（贡献利润/收入）';
COMMENT ON TABLE public.platform_fees IS '平台费用（佣金/仓储等）';
COMMENT ON TABLE public.refunds IS '退款记录';
COMMENT ON TABLE public.fx_rates IS '汇率（币种对 x 日期）';
COMMENT ON TABLE public.reconciliation_records IS '对账记录（平台 vs 系统收入差异）';

-- ---------- 31. 用户评论 / VOC ----------
COMMENT ON TABLE public.reviews IS '用户评论（rating 1-5，异常 SKU 归因的重要证据源）';
COMMENT ON COLUMN public.reviews.rating IS '评分（1-5）';
COMMENT ON COLUMN public.reviews.review_text IS '评论正文';
COMMENT ON TABLE public.review_aspects IS '评论方面维度（comfort/delivery/quality/price + 情感）';
COMMENT ON TABLE public.review_sentiments IS '评论情感分析结果';
COMMENT ON TABLE public.customer_feedback IS '客户反馈汇总（多渠道）';
COMMENT ON TABLE public.return_reasons IS '退货原因（reason_code + 详情）';

-- ---------- 32. Agent 审计表 ----------
COMMENT ON TABLE public.agent_runs IS 'Agent 运行总表（每次用户提问=一条 run）';
COMMENT ON COLUMN public.agent_runs.question IS '用户原始问题';
COMMENT ON COLUMN public.agent_runs.total_tokens IS '消耗 token 数';
COMMENT ON COLUMN public.agent_runs.total_cost IS '估算成本（USD）';
COMMENT ON TABLE public.agent_steps IS 'Agent 执行步骤（节点级审计）';
COMMENT ON TABLE public.agent_tool_calls IS 'Agent 工具调用记录（SQL/检索等）';
COMMENT ON TABLE public.agent_errors IS 'Agent 错误与重试记录';
COMMENT ON TABLE public.agent_interrupts IS 'Agent 人工中断/审批点';
COMMENT ON TABLE public.agent_results IS 'Agent 结构化结果（JSONB，供决策汇总）';

-- ---------- 34. 结构化长期记忆 ----------
COMMENT ON TABLE public.user_profiles IS '用户画像（key-value）';
COMMENT ON TABLE public.user_preferences IS '用户偏好（key-value）';
COMMENT ON TABLE public.business_preferences IS '业务级偏好（全局/分部门）';

-- ---------- 35-38. 知识库（PGVector） ----------
COMMENT ON TABLE public.knowledge_documents IS '知识文档（SOP/报告/产品规格/FAQ，按部门）';
COMMENT ON TABLE public.knowledge_chunks IS '知识分块（含 embedding 向量，用于语义检索）';
COMMENT ON COLUMN public.knowledge_chunks.embedding IS '向量嵌入（vector(1536)，text-embedding-3-small）';
COMMENT ON TABLE public.knowledge_embeddings IS '知识向量（按模型/维度分开存储）';

-- ---------- 22. Agent 元数据（口径 / 业务规则） ----------
COMMENT ON TABLE public.agent_metric_definition IS '指标口径定义（Agent 生成 SQL 时必须遵守）';
COMMENT ON COLUMN public.agent_metric_definition.metric_name IS '指标名（如 库存天数/贡献利润/ROAS）';
COMMENT ON COLUMN public.agent_metric_definition.formula IS '计算公式';
COMMENT ON COLUMN public.agent_metric_definition.unit IS '单位';
COMMENT ON TABLE public.agent_business_rule IS '业务规则（阈值/告警条件等）';

-- ---------- 46. Prompt 版本 ----------
COMMENT ON TABLE public.prompt_versions IS 'Prompt 版本管理（按 Agent + 版本 + content_hash）';

-- ---------- 47. Evaluation ----------
COMMENT ON TABLE public.evaluation_cases IS '评估用例（问题 + 期望 Agent/SQL/答案）';
COMMENT ON TABLE public.evaluation_runs IS '评估运行批次';
COMMENT ON TABLE public.evaluation_scores IS '评估得分（按 metric）';

-- ---------- mart 层（Agent 优先查询层） ----------
COMMENT ON TABLE public.mart_sales_daily IS '销售宽表（Agent 查销量/订单/GMV 的首选表）';
COMMENT ON COLUMN public.mart_sales_daily.date IS '日期（时间窗口过滤字段）';
COMMENT ON COLUMN public.mart_sales_daily.country IS '国家';
COMMENT ON COLUMN public.mart_sales_daily.brand_id IS '品牌 ID';
COMMENT ON COLUMN public.mart_sales_daily.gmv IS '成交金额（USD）';
COMMENT ON TABLE public.mart_product_profit_daily IS '商品利润宽表（毛利/贡献利润/利润率）';
COMMENT ON TABLE public.mart_ad_performance_daily IS '广告效果宽表（花费/ROAS/CTR/CVR）';
COMMENT ON TABLE public.mart_inventory_risk IS '库存风险宽表（库存天数/预测需求/风险等级）';
COMMENT ON COLUMN public.mart_inventory_risk.risk_level IS '风险等级（high/medium/low）';
COMMENT ON COLUMN public.mart_inventory_risk.stock_days IS '库存可售天数';
