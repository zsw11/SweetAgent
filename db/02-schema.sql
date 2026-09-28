-- ============================================================
-- 02-schema.sql 甜秘密 Multi-Agent 系统全量表结构
-- 对应设计文档 23-32 节（业务表）+ 32 节（Agent 审计表）
-- + 34-38 节（记忆/知识库）+ 46-47 节（Prompt 版本/评估）
-- 执行用户：app_user（表属主）；扩展需由超级用户先建（见 README）
-- 执行库：sweetnight_agent
-- 注释规范：表注释在建表语句上方一行，字段注释在字段行尾 -- 后
-- ============================================================

SET ROLE app_user;
SET search_path TO public;

-- ============================================================
-- 23.1 用户与组织
-- ============================================================

-- 部门（Operation/Logistics/Finance/Product 等）
CREATE TABLE IF NOT EXISTS departments (
    id          BIGSERIAL PRIMARY KEY,          -- 主键ID
    name        VARCHAR(100) NOT NULL,          -- 部门名称
    code        VARCHAR(50)  NOT NULL UNIQUE,   -- 部门编码
    description TEXT,                          -- 部门描述
    status      VARCHAR(20)  NOT NULL DEFAULT 'active',  -- 状态（active/inactive）
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),     -- 创建时间
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()      -- 更新时间
);

-- 系统用户（可关联部门，Agent 审计用）
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,           -- 主键ID
    username      VARCHAR(100) NOT NULL UNIQUE,    -- 登录用户名
    display_name  VARCHAR(100) NOT NULL,           -- 显示名称
    department_id BIGINT REFERENCES departments(id), -- 所属部门ID
    email         VARCHAR(200),                    -- 邮箱
    status        VARCHAR(20)  NOT NULL DEFAULT 'active',  -- 状态（active/inactive）
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),     -- 创建时间
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()      -- 更新时间
);
select * from users;
-- 用户-角色关联
CREATE TABLE IF NOT EXISTS user_roles (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 用户ID
    role_name  VARCHAR(50) NOT NULL,           -- 角色名（admin/operator/analyst 等）
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 角色权限定义
CREATE TABLE IF NOT EXISTS role_permissions (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    role_name  VARCHAR(50) NOT NULL,           -- 角色名
    permission VARCHAR(100) NOT NULL,          -- 权限标识（如 chat:write / reports:read）
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- ============================================================
-- 24. 平台 / 市场 / 店铺
-- ============================================================

-- 销售平台（Amazon/Walmart/官网等）
CREATE TABLE IF NOT EXISTS platforms (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    code       VARCHAR(50) NOT NULL UNIQUE,     -- 平台编码（AMZ/WMT/SHOPIFY）
    name       VARCHAR(100) NOT NULL,           -- 平台名称
    status     VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 目标市场（US/CA/UK 等，含币种）
CREATE TABLE IF NOT EXISTS markets (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    code       VARCHAR(10) NOT NULL UNIQUE,     -- 市场编码（US/CA/UK）
    name       VARCHAR(100) NOT NULL,           -- 市场名称
    currency   VARCHAR(10) NOT NULL,            -- 结算币种（USD/CAD/GBP）
    status     VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 品牌（1=SweetNight 2=Novilla 3=Avenco 4=甜秘密）
CREATE TABLE IF NOT EXISTS brands (
    id          BIGSERIAL PRIMARY KEY,           -- 主键ID
    name        VARCHAR(100) NOT NULL UNIQUE,    -- 品牌名（Agent 过滤品牌时按此字段匹配）
    description TEXT,                            -- 品牌描述
    status      VARCHAR(20)  NOT NULL DEFAULT 'active',  -- 状态
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),     -- 创建时间
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()      -- 更新时间
);

-- 店铺（平台 x 品牌 x 国家）
CREATE TABLE IF NOT EXISTS stores (
    id          BIGSERIAL PRIMARY KEY,           -- 主键ID
    platform_id BIGINT NOT NULL REFERENCES platforms(id),  -- 所属平台ID
    brand_id    BIGINT NOT NULL REFERENCES brands(id),     -- 所属品牌ID
    country_code VARCHAR(10) NOT NULL,          -- 店铺所在国家（US/CA）
    store_name  VARCHAR(200) NOT NULL,          -- 店铺名称
    store_type  VARCHAR(50) NOT NULL DEFAULT 'marketplace',  -- 店铺类型（marketplace/self-operated）
    status      VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);


-- 店铺第三方平台账号（Token 仅存密文，Agent 不持有明文）
CREATE TABLE IF NOT EXISTS store_accounts (
    id                BIGSERIAL PRIMARY KEY,           -- 主键ID
    store_id          BIGINT NOT NULL REFERENCES stores(id),  -- 所属店铺ID
    account_name      VARCHAR(200) NOT NULL,           -- 账号名称
    api_key_encrypted TEXT,                          -- 第三方平台 Token 密文，Agent 不持有明文
    status            VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- ============================================================
-- 26. 商品
-- ============================================================

-- 商品类目（支持父子层级）
CREATE TABLE IF NOT EXISTS product_categories (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    parent_id  BIGINT REFERENCES product_categories(id),  -- 父类目ID（NULL 为一级类目）
    name       VARCHAR(100) NOT NULL,           -- 类目名称
    code       VARCHAR(50)  NOT NULL UNIQUE,    -- 类目编码
    status     VARCHAR(20) NOT NULL DEFAULT 'active'   -- 状态
);

-- 商品主档（挂品牌/类目）
CREATE TABLE IF NOT EXISTS products (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    brand_id     BIGINT NOT NULL REFERENCES brands(id),     -- 所属品牌ID
    category_id  BIGINT REFERENCES product_categories(id), -- 所属类目ID
    product_name VARCHAR(200) NOT NULL,           -- 商品名称
    product_type VARCHAR(50),                     -- 商品类型（mattress/pillow/bedding）
    status       VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态（active/discontinued）
    launch_date  DATE,                            -- 上架日期
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),     -- 创建时间
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()      -- 更新时间
);

-- SKU 主档（核心表：SKU 编码/国家/售价/成本）
CREATE TABLE IF NOT EXISTS product_skus (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    product_id   BIGINT NOT NULL REFERENCES products(id),  -- 所属商品ID
    sku_code     VARCHAR(100) NOT NULL UNIQUE,    -- SKU 编码（如 SN-Q12-US，前缀为品牌缩写，不含品牌全名）
    country_code VARCHAR(10) NOT NULL,            -- 销售国家（US/CA/UK）
    sale_price   NUMERIC(12,2) NOT NULL,          -- 售价（USD）
    cost         NUMERIC(12,2) NOT NULL,          -- 成本（USD）
    weight       NUMERIC(10,2),                   -- 重量（kg）
    status       VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- SKU 分国家价格历史（带生效区间）
CREATE TABLE IF NOT EXISTS product_prices (
    id             BIGSERIAL PRIMARY KEY,           -- 主键ID
    sku_id         BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    country_code   VARCHAR(10) NOT NULL,            -- 国家
    currency       VARCHAR(10) NOT NULL,            -- 币种
    price          NUMERIC(12,2) NOT NULL,          -- 价格
    effective_from DATE NOT NULL,                  -- 生效起始日
    effective_to   DATE                             -- 生效截止日（NULL=当前生效）
);

-- SKU 分类型成本历史（product/shipping/ad 等）
CREATE TABLE IF NOT EXISTS product_costs (
    id             BIGSERIAL PRIMARY KEY,           -- 主键ID
    sku_id         BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    cost_type      VARCHAR(50) NOT NULL DEFAULT 'product',  -- 成本类型（product/shipping/ad）
    amount         NUMERIC(12,2) NOT NULL,          -- 金额
    currency       VARCHAR(10) NOT NULL DEFAULT 'USD',  -- 币种
    effective_from DATE NOT NULL,                  -- 生效起始日
    effective_to   DATE                             -- 生效截止日
);

-- 商品生命周期阶段（new/growth/mature/decline/eol）
CREATE TABLE IF NOT EXISTS product_lifecycle (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    sku_id     BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    stage      VARCHAR(50) NOT NULL,           -- 阶段（new/growth/mature/decline/eol）
    start_date DATE NOT NULL,                  -- 进入该阶段日期
    end_date   DATE,                            -- 离开该阶段日期
    notes      TEXT                             -- 备注
);

-- 商品开发项目（含计划/实际上架日期）
CREATE TABLE IF NOT EXISTS product_development_projects (
    id                  BIGSERIAL PRIMARY KEY,           -- 主键ID
    name                VARCHAR(200) NOT NULL,           -- 项目名称
    brand_id            BIGINT REFERENCES brands(id),    -- 所属品牌ID
    status              VARCHAR(20) NOT NULL DEFAULT 'draft',  -- 状态（draft/in_progress/launched）
    stage               VARCHAR(50),                     -- 当前阶段
    owner_user_id       BIGINT REFERENCES users(id),     -- 负责人ID
    planned_launch_date DATE,                             -- 计划上架日期
    actual_launch_date  DATE,                             -- 实际上架日期
    description         TEXT,                             -- 项目描述
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()  -- 创建时间
);

-- ============================================================
-- 27. 销售
-- ============================================================

-- 订单（平台原始订单汇总）
CREATE TABLE IF NOT EXISTS orders (
    id               BIGSERIAL PRIMARY KEY,           -- 主键ID
    platform_order_id VARCHAR(100),                   -- 平台订单号
    store_id         BIGINT NOT NULL REFERENCES stores(id),  -- 所属店铺ID
    country_code     VARCHAR(10) NOT NULL,            -- 下单国家
    order_date       DATE NOT NULL,                   -- 下单日期
    currency         VARCHAR(10) NOT NULL DEFAULT 'USD',  -- 币种
    order_amount     NUMERIC(12,2) NOT NULL,          -- 订单金额（含税）
    refund_amount    NUMERIC(12,2) NOT NULL DEFAULT 0,  -- 退款金额
    status           VARCHAR(20) NOT NULL DEFAULT 'completed',  -- 订单状态
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 订单明细（SKU 维度）
CREATE TABLE IF NOT EXISTS order_items (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    order_id     BIGINT NOT NULL REFERENCES orders(id),  -- 订单ID
    sku_id       BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    quantity     INT NOT NULL,                    -- 购买数量
    sale_amount  NUMERIC(12,2) NOT NULL,          -- 销售金额
    discount     NUMERIC(12,2) NOT NULL DEFAULT 0,  -- 折扣金额
    refund_amount NUMERIC(12,2) NOT NULL DEFAULT 0  -- 退款金额
);

-- 销售日报（明细层：日期x店铺xSKU 的订单/销量/GMV）
CREATE TABLE IF NOT EXISTS sales_daily (
    date          DATE NOT NULL,                  -- 日期（Agent 时间窗口过滤字段）
    country       VARCHAR(10) NOT NULL,           -- 国家（US/CA/UK）
    brand_id      BIGINT REFERENCES brands(id),   -- 品牌 ID（关联 brands）
    store_id      BIGINT NOT NULL REFERENCES stores(id),  -- 店铺ID
    sku_id        BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    orders        INT NOT NULL DEFAULT 0,         -- 订单数
    units         INT NOT NULL DEFAULT 0,         -- 销量（件）
    gmv           NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 成交金额（USD）
    refund_amount NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 退款金额
    PRIMARY KEY (date, store_id, sku_id)            -- 联合主键
);

-- ============================================================
-- 28. 广告
-- ============================================================

-- 广告活动（campaign 层：预算/目标/起止）
CREATE TABLE IF NOT EXISTS ad_campaigns (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    store_id     BIGINT NOT NULL REFERENCES stores(id),  -- 所属店铺ID
    platform     VARCHAR(50) NOT NULL,           -- 平台（AMZ_ADS/TIKTOK）
    campaign_name VARCHAR(200) NOT NULL,          -- 活动名（如 SN US Brand Defense）
    objective    VARCHAR(50),                    -- 广告目标（traffic/conversion）
    status       VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态
    start_date   DATE,                            -- 开始日期
    end_date     DATE,                            -- 结束日期
    daily_budget NUMERIC(12,2),                   -- 每日预算（USD）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 广告组（campaign 下分组）
CREATE TABLE IF NOT EXISTS ad_groups (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    campaign_id  BIGINT NOT NULL REFERENCES ad_campaigns(id),  -- 所属广告活动ID
    ad_group_name VARCHAR(200) NOT NULL,          -- 广告组名称
    status       VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 广告创意（图片/视频等素材）
CREATE TABLE IF NOT EXISTS ad_creatives (
    id            BIGSERIAL PRIMARY KEY,           -- 主键ID
    ad_group_id   BIGINT NOT NULL REFERENCES ad_groups(id),  -- 所属广告组ID
    creative_name VARCHAR(200) NOT NULL,          -- 创意名称
    creative_type VARCHAR(50) NOT NULL DEFAULT 'image',  -- 创意类型（image/video）
    status        VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 广告效果日报（impressions/clicks/spend/conversions/revenue + CTR/CVR/CPC/ROAS）
CREATE TABLE IF NOT EXISTS ad_performance_daily (
    date         DATE NOT NULL,                   -- 日期
    platform     VARCHAR(50) NOT NULL,            -- 平台
    store_id     BIGINT NOT NULL REFERENCES stores(id),  -- 店铺ID
    campaign_id  BIGINT NOT NULL REFERENCES ad_campaigns(id),  -- 广告活动ID
    sku_id       BIGINT REFERENCES product_skus(id),  -- SKU ID
    impressions  BIGINT NOT NULL DEFAULT 0,       -- 曝光量
    clicks       INT NOT NULL DEFAULT 0,          -- 点击量
    spend        NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 广告花费（USD）
    conversions  INT NOT NULL DEFAULT 0,          -- 转化数
    revenue      NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 广告带来收入（USD）
    ctr          NUMERIC(8,4),                   -- 点击率=clicks/impressions
    cvr          NUMERIC(8,4),                   -- 转化率=conversions/clicks
    cpc          NUMERIC(10,4),                  -- 单次点击成本=spend/clicks
    roas         NUMERIC(10,4),                  -- ROAS=revenue/spend
    PRIMARY KEY (date, campaign_id, sku_id)         -- 联合主键
);

-- ============================================================
-- 29. 库存 / 物流
-- ============================================================

-- 仓库（分国家/区域）
CREATE TABLE IF NOT EXISTS warehouses (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    code         VARCHAR(50) NOT NULL UNIQUE,     -- 仓库编码
    name         VARCHAR(200) NOT NULL,           -- 仓库名称
    country_code VARCHAR(10) NOT NULL,            -- 所在国家
    region       VARCHAR(50),                    -- 区域（west/east）
    status       VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 物流承运商
CREATE TABLE IF NOT EXISTS carriers (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    code         VARCHAR(50) NOT NULL UNIQUE,     -- 承运商编码（UPS/FedEx/USPS）
    name         VARCHAR(100) NOT NULL,           -- 承运商名称
    service_level VARCHAR(50),                    -- 服务等级（ground/express/overnight）
    status       VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 实时库存快照（warehouse x SKU）
CREATE TABLE IF NOT EXISTS inventory (
    id            BIGSERIAL PRIMARY KEY,           -- 主键ID
    warehouse_id  BIGINT NOT NULL REFERENCES warehouses(id),  -- 仓库ID
    sku_id        BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    available_qty INT NOT NULL DEFAULT 0,         -- 可售库存
    reserved_qty  INT NOT NULL DEFAULT 0,         -- 已预留库存（已下单未发货）
    in_transit_qty INT NOT NULL DEFAULT 0,        -- 在途库存
    safety_stock  INT NOT NULL DEFAULT 0,         -- 安全库存阈值
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 最后更新时间
    UNIQUE (warehouse_id, sku_id)                  -- 联合唯一约束
);

-- 库存日报（含 stock_days 库存可售天数）
CREATE TABLE IF NOT EXISTS inventory_daily (
    date           DATE NOT NULL,                  -- 日期
    warehouse_id   BIGINT NOT NULL REFERENCES warehouses(id),  -- 仓库ID
    sku_id         BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    available_qty  INT NOT NULL DEFAULT 0,         -- 可售库存
    reserved_qty   INT NOT NULL DEFAULT 0,         -- 已预留库存
    in_transit_qty INT NOT NULL DEFAULT 0,         -- 在途库存
    safety_stock   INT NOT NULL DEFAULT 0,         -- 安全库存阈值
    stock_days     NUMERIC(10,2),                  -- 库存天数（可售天数，低于 12 天为高风险）
    PRIMARY KEY (date, warehouse_id, sku_id)       -- 联合主键
);

-- 入库单（在途补货）
CREATE TABLE IF NOT EXISTS inbound_shipments (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id),  -- 目标仓库ID
    sku_id       BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    quantity     INT NOT NULL,                    -- 入库数量
    eta_date     DATE,                            -- 预计到货日
    status       VARCHAR(20) NOT NULL DEFAULT 'in_transit',  -- 状态（in_transit/received）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 出库单（发货）
CREATE TABLE IF NOT EXISTS outbound_shipments (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    order_id     BIGINT REFERENCES orders(id),    -- 关联订单ID
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id),  -- 发货仓库ID
    sku_id       BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    quantity     INT NOT NULL,                    -- 出库数量
    ship_date    DATE,                            -- 发货日期
    status       VARCHAR(20) NOT NULL DEFAULT 'pending',  -- 状态（pending/shipped）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 物流运单（承运商/轨迹）
CREATE TABLE IF NOT EXISTS logistics_orders (
    id               BIGSERIAL PRIMARY KEY,           -- 主键ID
    order_id         BIGINT REFERENCES orders(id),   -- 关联订单ID
    carrier_id       BIGINT NOT NULL REFERENCES carriers(id),  -- 承运商ID
    tracking_number  VARCHAR(100),                   -- 物流追踪号
    warehouse_id     BIGINT REFERENCES warehouses(id),  -- 发货仓库ID
    ship_date        DATE,                            -- 发货日期
    delivery_date    DATE,                            -- 妥投日期
    status           VARCHAR(20) NOT NULL DEFAULT 'created',  -- 状态（created/in_transit/delivered）
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 物流成本明细
CREATE TABLE IF NOT EXISTS logistics_cost (
    id          BIGSERIAL PRIMARY KEY,           -- 主键ID
    order_id    BIGINT REFERENCES orders(id),    -- 关联订单ID
    carrier_id  BIGINT REFERENCES carriers(id),  -- 承运商ID
    cost_type   VARCHAR(50) NOT NULL DEFAULT 'shipping',  -- 成本类型（shipping/handling/storage）
    amount      NUMERIC(12,2) NOT NULL,          -- 金额
    currency    VARCHAR(10) NOT NULL DEFAULT 'USD',  -- 币种
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 物流轨迹事件（揽收/运输/妥投等）
CREATE TABLE IF NOT EXISTS tracking_events (
    id                 BIGSERIAL PRIMARY KEY,           -- 主键ID
    logistics_order_id BIGINT NOT NULL REFERENCES logistics_orders(id),  -- 物流运单ID
    event_code         VARCHAR(50),                    -- 事件编码
    event_name         VARCHAR(200),                   -- 事件名称
    event_date         TIMESTAMPTZ,                    -- 事件时间
    location           VARCHAR(200),                   -- 事件地点
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- ============================================================
-- 30. 财务
-- ============================================================

-- 财务流水（revenue/fee/refund/logistics/advertising）
CREATE TABLE IF NOT EXISTS financial_transactions (
    id               BIGSERIAL PRIMARY KEY,           -- 主键ID
    store_id         BIGINT NOT NULL REFERENCES stores(id),  -- 店铺ID
    transaction_date DATE NOT NULL,                  -- 交易日期
    transaction_type VARCHAR(50) NOT NULL,            -- 类型（revenue/fee/refund/logistics/advertising）
    amount           NUMERIC(14,2) NOT NULL,          -- 金额
    currency         VARCHAR(10) NOT NULL DEFAULT 'USD',  -- 币种
    fx_rate          NUMERIC(12,6) DEFAULT 1,         -- 汇率（折算到 USD）
    description      TEXT,                            -- 交易描述
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 收入日报（店铺维度）
CREATE TABLE IF NOT EXISTS revenue_daily (
    date      DATE NOT NULL,                       -- 日期
    store_id  BIGINT NOT NULL REFERENCES stores(id),  -- 店铺ID
    country   VARCHAR(10) NOT NULL,                -- 国家
    revenue   NUMERIC(14,2) NOT NULL DEFAULT 0,    -- 收入金额
    currency  VARCHAR(10) NOT NULL DEFAULT 'USD',  -- 币种
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), -- 创建时间
    PRIMARY KEY (date, store_id)                    -- 联合主键
);

-- 成本日报（按成本类型）
CREATE TABLE IF NOT EXISTS cost_daily (
    date      DATE NOT NULL,                       -- 日期
    store_id  BIGINT NOT NULL REFERENCES stores(id),  -- 店铺ID
    cost_type VARCHAR(50) NOT NULL,                -- 成本类型（ad/logistics/platform_fee/refund）
    amount    NUMERIC(14,2) NOT NULL DEFAULT 0,    -- 成本金额
    currency  VARCHAR(10) NOT NULL DEFAULT 'USD',  -- 币种
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), -- 创建时间
    PRIMARY KEY (date, store_id, cost_type)         -- 联合主键
);

-- 利润日报（毛利/贡献利润/利润率）
CREATE TABLE IF NOT EXISTS profit_daily (
    date               DATE NOT NULL,                  -- 日期
    store_id           BIGINT NOT NULL REFERENCES stores(id),  -- 店铺ID
    sku_id             BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    revenue            NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 收入
    product_cost       NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 产品成本
    platform_fee       NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 平台费用
    advertising_cost  NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 广告成本
    logistics_cost     NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 物流成本
    refund_cost        NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 退款成本
    gross_profit       NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 毛利=revenue-product_cost
    contribution_profit NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 贡献利润=毛利-平台费-广告-物流-退款
    profit_margin      NUMERIC(10,4),                  -- 利润率（贡献利润/收入）
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 创建时间
    PRIMARY KEY (date, store_id, sku_id)            -- 联合主键
);

-- 平台费用（佣金/仓储等）
CREATE TABLE IF NOT EXISTS platform_fees (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    order_id   BIGINT REFERENCES orders(id),    -- 关联订单ID
    store_id   BIGINT NOT NULL REFERENCES stores(id),  -- 店铺ID
    fee_type   VARCHAR(50) NOT NULL,            -- 费用类型（commission/fba_storage/referral）
    amount     NUMERIC(14,2) NOT NULL,          -- 金额
    currency   VARCHAR(10) NOT NULL DEFAULT 'USD',  -- 币种
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 退款记录
CREATE TABLE IF NOT EXISTS refunds (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    order_id     BIGINT REFERENCES orders(id),    -- 关联订单ID
    sku_id       BIGINT REFERENCES product_skus(id),  -- SKU ID
    refund_amount NUMERIC(12,2) NOT NULL,         -- 退款金额
    refund_date  DATE NOT NULL,                   -- 退款日期
    reason       VARCHAR(200),                    -- 退款原因
    status       VARCHAR(20) NOT NULL DEFAULT 'completed',  -- 状态
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 汇率（币种对 x 日期）
CREATE TABLE IF NOT EXISTS fx_rates (
    currency_pair VARCHAR(10) NOT NULL,           -- 币种对（如 USD/EUR）
    rate_date     DATE NOT NULL,                   -- 汇率日期
    rate          NUMERIC(12,6) NOT NULL,          -- 汇率值
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 创建时间
    PRIMARY KEY (currency_pair, rate_date)          -- 联合主键
);

-- 对账记录（平台 vs 系统收入差异）
CREATE TABLE IF NOT EXISTS reconciliation_records (
    id               BIGSERIAL PRIMARY KEY,           -- 主键ID
    period_start     DATE NOT NULL,                   -- 对账周期起始
    period_end       DATE NOT NULL,                   -- 对账周期结束
    store_id         BIGINT NOT NULL REFERENCES stores(id),  -- 店铺ID
    platform_revenue NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 平台报表收入
    system_revenue   NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 系统记录收入
    difference       NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 差异金额
    status           VARCHAR(20) NOT NULL DEFAULT 'pending',  -- 状态（pending/resolved）
    reconciled_at    TIMESTAMPTZ,                    -- 对账完成时间
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()  -- 创建时间
);

-- ============================================================
-- 31. 用户评论 / VOC
-- ============================================================

-- 用户评论（rating 1-5，异常 SKU 归因的重要证据源）
CREATE TABLE IF NOT EXISTS reviews (
    id                BIGSERIAL PRIMARY KEY,           -- 主键ID
    platform          VARCHAR(50) NOT NULL,            -- 来源平台
    sku_id            BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    country           VARCHAR(10) NOT NULL,            -- 评论所在国家
    rating            INT NOT NULL CHECK (rating BETWEEN 1 AND 5),  -- 评分（1-5）
    review_text       TEXT,                            -- 评论正文
    review_date       DATE NOT NULL,                   -- 评论日期
    verified_purchase BOOLEAN DEFAULT true,            -- 是否验证购买
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 评论方面维度（comfort/delivery/quality/price + 情感）
CREATE TABLE IF NOT EXISTS review_aspects (
    id        BIGSERIAL PRIMARY KEY,           -- 主键ID
    review_id BIGINT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,  -- 评论ID
    aspect    VARCHAR(50) NOT NULL,            -- 方面（comfort/delivery/quality/price）
    sentiment VARCHAR(20) NOT NULL,            -- 情感（positive/neutral/negative）
    score     NUMERIC(4,2)                     -- 情感得分
);

-- 评论情感分析结果
CREATE TABLE IF NOT EXISTS review_sentiments (
    id             BIGSERIAL PRIMARY KEY,           -- 主键ID
    review_id      BIGINT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,  -- 评论ID
    sentiment_type VARCHAR(50) NOT NULL,            -- 情感类型
    score          NUMERIC(6,4),                    -- 情感得分
    confidence     NUMERIC(6,4)                     -- 置信度
);

-- 客户反馈汇总（多渠道）
CREATE TABLE IF NOT EXISTS customer_feedback (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    source     VARCHAR(50) NOT NULL,            -- 反馈来源（email/chat/call）
    content    TEXT,                            -- 反馈内容
    sentiment  VARCHAR(20),                     -- 情感倾向
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- 退货原因（reason_code + 详情）
CREATE TABLE IF NOT EXISTS return_reasons (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    order_id     BIGINT REFERENCES orders(id),    -- 关联订单ID
    sku_id       BIGINT REFERENCES product_skus(id),  -- SKU ID
    reason_code  VARCHAR(50),                     -- 退货原因编码
    reason_detail TEXT,                           -- 退货原因详情
    return_date  DATE,                            -- 退货日期
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- ============================================================
-- 32. Agent 审计表
-- ============================================================

-- Agent 运行总表（每次用户提问=一条 run）
CREATE TABLE IF NOT EXISTS agent_runs (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    thread_id    VARCHAR(100),                    -- 会话线程ID（checkpoint 关联）
    user_id      BIGINT REFERENCES users(id),     -- 用户ID
    question     TEXT NOT NULL,                   -- 用户原始问题
    status       VARCHAR(20) NOT NULL DEFAULT 'running',  -- 运行状态
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 开始时间
    finished_at  TIMESTAMPTZ,                     -- 结束时间
    total_tokens INT NOT NULL DEFAULT 0,          -- 消耗 token 数
    total_cost   NUMERIC(12,6) NOT NULL DEFAULT 0  -- 估算成本（USD）
);
select *from agent_runs;

-- Agent 执行步骤（节点级审计）
CREATE TABLE IF NOT EXISTS agent_steps (
    id            BIGSERIAL PRIMARY KEY,           -- 主键ID
    run_id        BIGINT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,  -- 运行ID
    agent_name    VARCHAR(50) NOT NULL,            -- Agent 名称（manager/operation/decision）
    node_name     VARCHAR(100),                    -- 节点名称
    step_index    INT NOT NULL DEFAULT 0,          -- 步骤序号
    input_summary TEXT,                            -- 输入摘要
    output_summary TEXT,                           -- 输出摘要
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 开始时间
    finished_at   TIMESTAMPTZ,                     -- 结束时间
    status        VARCHAR(20) NOT NULL DEFAULT 'running'  -- 状态
);

-- Agent 工具调用记录（SQL/检索等）
CREATE TABLE IF NOT EXISTS agent_tool_calls (
    id            BIGSERIAL PRIMARY KEY,           -- 主键ID
    run_id        BIGINT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,  -- 运行ID
    agent_name    VARCHAR(50) NOT NULL,            -- Agent 名称
    tool_name     VARCHAR(100) NOT NULL,           -- 工具名（execute_readonly_sql/generate_sql）
    arguments     JSONB,                           -- 调用参数
    result_summary TEXT,                           -- 结果摘要
    duration_ms   INT,                             -- 耗时（毫秒）
    status        VARCHAR(20) NOT NULL DEFAULT 'success',  -- 状态
    error_message TEXT,                            -- 错误信息
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- Agent 错误与重试记录
CREATE TABLE IF NOT EXISTS agent_errors (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    run_id       BIGINT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,  -- 运行ID
    agent_name   VARCHAR(50),                     -- Agent 名称
    node_name    VARCHAR(100),                    -- 节点名称
    error_type   VARCHAR(50),                     -- 错误类型
    error_message TEXT,                           -- 错误信息
    retry_count  INT NOT NULL DEFAULT 0,          -- 重试次数
    stack_trace  TEXT,                            -- 堆栈
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- Agent 人工中断/审批点
CREATE TABLE IF NOT EXISTS agent_interrupts (
    id            BIGSERIAL PRIMARY KEY,           -- 主键ID
    run_id        BIGINT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,  -- 运行ID
    thread_id     VARCHAR(100),                    -- 会话线程ID
    agent_name    VARCHAR(50),                     -- Agent 名称
    interrupt_type VARCHAR(50),                    -- 中断类型（missing_param/high_risk_action）
    payload       JSONB,                           -- 中断载荷
    status        VARCHAR(20) NOT NULL DEFAULT 'pending',  -- 状态（pending/resolved）
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 创建时间
    resolved_at   TIMESTAMPTZ,                     -- 解决时间
    resolved_by   BIGINT REFERENCES users(id),     -- 解决人ID
    resolution    JSONB                            -- 解决结果
);

-- Agent 结构化结果（JSONB，供决策汇总）
CREATE TABLE IF NOT EXISTS agent_results (
    id          BIGSERIAL PRIMARY KEY,           -- 主键ID
    run_id      BIGINT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,  -- 运行ID
    agent_name  VARCHAR(50) NOT NULL,            -- Agent 名称
    result_type VARCHAR(50) NOT NULL DEFAULT 'department_result',  -- 结果类型
    result      JSONB NOT NULL,                  -- 结构化结果（DecisionOutput 等）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);

-- ============================================================
-- 34. 结构化长期记忆
-- ============================================================

-- 用户画像（key-value）可以加版本表，用来记录旧的记忆版本链，当用户问我之前，为什么这次不一样等场景，查询旧的记忆，用一次注入就丢掉
CREATE TABLE IF NOT EXISTS user_profiles (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 用户ID
    key        VARCHAR(100) NOT NULL,           -- 属性键
    value      TEXT,                            -- 属性值
    confidence REAL,                            -- 置信度（LLM 自评，写入前已按阈值过滤；存留供审计/未来冲突检测）
    evidence   TEXT,                            -- 依据（用户原话/来源）
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 首次确认时间（覆盖时保留）
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 最近更新时间
    UNIQUE (user_id, key)                       -- 联合唯一约束
);

-- 用户偏好（key-value）
CREATE TABLE IF NOT EXISTS user_preferences (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 用户ID
    key        VARCHAR(100) NOT NULL,           -- 偏好键（default_market/currency/time_range）
    value      TEXT,                            -- 偏好值
    evidence   TEXT,                            -- 依据（用户原话/来源）
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 首次确认时间（覆盖时保留）
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 最近更新时间
    UNIQUE (user_id, key)                       -- 联合唯一约束
);

-- 业务级偏好（全局/分部门）
CREATE TABLE IF NOT EXISTS business_preferences (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    key        VARCHAR(100) NOT NULL,           -- 偏好键
    value      TEXT,                            -- 偏好值
    scope      VARCHAR(50) NOT NULL DEFAULT 'global',  -- 作用域（global/operation/finance）
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 更新时间
    UNIQUE (key, scope)                         -- 联合唯一约束
);
select * from user_memories where user_id = 14;
-- 用户非结构化记忆（设计文档 35 节：语义长期记忆）
-- 存：偏好语义 / 事实 / 历史结论 / 业务规则（自由文本 + 向量），用户私有，检索优先选向量相近且执行度高的记忆top5
-- department 标签用于"检索按任务过滤"（无标签 = 通用记忆，不参与粗筛淘汰）
CREATE TABLE IF NOT EXISTS user_memories (
    id            BIGSERIAL PRIMARY KEY,           -- 主键ID
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 用户ID
    memory_type   VARCHAR(20) NOT NULL DEFAULT 'preference',  -- 类型（preference/fact/conclusion/rule）
    content       TEXT NOT NULL,                   -- 记忆内容（自由文本）
    department    VARCHAR(50),                     -- 部门标签（operation/finance/logistics/product；空=通用记忆，不参与粗筛淘汰）
    metadata      JSONB,                           -- 元数据（topic 等扩展标签；department 已拆列，不再存这里）
    confidence    REAL,                            -- 置信度（LLM 自评 + evidence 约束）
    evidence      TEXT,                            -- 依据（用户原话/来源）
    embedding     vector(1536),                    -- 向量（当前为模拟向量，见 app/memory/embeddings.py）
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 创建时间
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 更新时间（同主题覆盖时刷新）
    superseded_at TIMESTAMPTZ                       -- 取代时间（冲突取代/软删除，保留历史）
);
CREATE INDEX IF NOT EXISTS user_memories_user_idx ON user_memories(user_id);
CREATE INDEX IF NOT EXISTS user_memories_dept_idx ON user_memories(department);

-- ============================================================
-- 34.1 多轮会话历史（OPT-12：上下文注入 / query 改写 / token 统计 / 压缩）
-- ============================================================

-- 会话消息流：按 thread_id 存"用户问 + 系统答"，区别于 checkpoints（图执行快照，仅供中断恢复）
-- role: user=用户问题 / assistant=系统最终答案 / summary=历史压缩摘要（旧消息压缩后的产物）
-- seq: 会话内单调递增序号，用于排序与压缩定位（保留最近 N 轮、更早的压成 summary）
CREATE TABLE IF NOT EXISTS conversation_messages (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    thread_id  VARCHAR(128) NOT NULL,            -- 会话ID（与 API thread_id 一致，核心隔离维度）
    user_id    VARCHAR(128) NOT NULL DEFAULT 'default',  -- 用户ID（字符串，轻量，不强制 FK users）
    role       VARCHAR(16) NOT NULL,             -- 角色（user/assistant/summary）
    content    TEXT NOT NULL,                    -- 消息内容
    tokens     INTEGER NOT NULL DEFAULT 0,       -- 该消息 token 数（压缩/提取阈值统计）
    seq        INTEGER NOT NULL,                 -- 会话内序号（单调递增）
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 创建时间
    UNIQUE (thread_id, seq)                      -- 同一会话内序号唯一
);
CREATE INDEX IF NOT EXISTS conv_thread_idx ON conversation_messages(thread_id, seq);

-- ============================================================
-- 35-38. 知识库（PGVector）向量召回 - 多个关键词兜底（混合检索）
-- ============================================================

-- 知识文档（SOP/报告/产品规格/FAQ，按部门） 一个文档一行
CREATE TABLE IF NOT EXISTS knowledge_documents (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    title        VARCHAR(300) NOT NULL,            -- 文档标题
    source_type  VARCHAR(50) NOT NULL,            -- 来源类型（SOP/report/product_spec/faq）
    department   VARCHAR(50) NOT NULL,            -- 所属部门（operation/logistics/finance/product）
    brand        VARCHAR(100),                    -- 关联品牌
    market       VARCHAR(50),                     -- 关联市场
    version      VARCHAR(50),                     -- 文档版本（当前仅展示元数据，未参与唯一键/检索；演进B：同title不同版本并存、检索取最新，见 development_log 待办18）
    content_hash VARCHAR(64),                     -- 全文 SHA-256 内容指纹（变更检测：同 title 同哈希跳过重建）
    status       VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);
select  * from knowledge_chunks where document_id = '8';
-- 知识分块（含 embedding 向量，用于语义检索）   一个文档切 N 块、N 行,导入时切块（按段落 / 字数）→ 每块生成向量一并写入。**查询主力表**
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          BIGSERIAL PRIMARY KEY,           -- 主键ID
    document_id BIGINT NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,  -- 文档ID
    chunk_index INT NOT NULL,                    -- 分块序号
    content     TEXT NOT NULL,                   -- 分块内容
    metadata    JSONB,                           -- 元数据（department/brand/market/version）
    embedding   vector(1536),                    -- 向量嵌入（text-embedding-3-small）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);
select * from knowledge_chunks;
-- 知识向量（按模型/维度分开存储）  嵌入时写入。**用途：同一文本可存多模型向量**，模型升级 / 换模型时可对比、可追溯，不用动 chunks
CREATE TABLE IF NOT EXISTS knowledge_embeddings (
    id         BIGSERIAL PRIMARY KEY,           -- 主键ID
    chunk_id   BIGINT NOT NULL REFERENCES knowledge_chunks(id) ON DELETE CASCADE,  -- 分块ID
    model      VARCHAR(100) NOT NULL,           -- 嵌入模型名
    dimension  INT NOT NULL,                    -- 向量维度
    embedding  vector(1536) NOT NULL,           -- 向量值
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);
select * from knowledge_embeddings;
-- ============================================================
-- 22. Agent 元数据（口径 / 业务规则）
-- ============================================================

-- 指标口径定义（Agent 生成 SQL 时必须遵守）
CREATE TABLE IF NOT EXISTS agent_metric_definition (
    id          BIGSERIAL PRIMARY KEY,           -- 主键ID
    metric_name VARCHAR(100) NOT NULL UNIQUE,   -- 指标名（如 库存天数/贡献利润/ROAS）
    definition  TEXT,                            -- 指标定义说明
    formula     TEXT,                            -- 计算公式
    unit        VARCHAR(50),                     -- 单位
    department  VARCHAR(50) NOT NULL,            -- 所属部门
    version     VARCHAR(50) NOT NULL DEFAULT 'v1',  -- 版本
    status      VARCHAR(20) NOT NULL DEFAULT 'active',  -- 状态
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);
select * from agent_metric_definition;
-- 业务规则（阈值/告警条件等）配置表
CREATE TABLE IF NOT EXISTS agent_business_rule (
    id          BIGSERIAL PRIMARY KEY,           -- 主键ID
    rule_name   VARCHAR(200) NOT NULL,           -- 规则名称
    rule_type   VARCHAR(50) NOT NULL,            -- 规则类型
    description TEXT,                            -- 规则描述
    department  VARCHAR(50) NOT NULL,            -- 所属部门
    is_active   BOOLEAN NOT NULL DEFAULT true,   -- 是否启用
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);
select * from agent_business_rule;
-- ============================================================
-- 46. Prompt 版本
-- **解决的问题**：Manager/Operation/Decision 三个 Agent 的 system prompt 和 task prompt 都写死在代码里（`prompts.py`）。改一次 prompt 就影响所有用户，但你不知道 "这个版本的 prompt 效果好不好"，出了问题也回不到旧版本。
-- **这张表做什么**：把每次部署的 prompt 内容落库，记录版本和生效状态。

-- ============================================================

-- Prompt 版本管理（按 Agent + 版本 + content_hash）
CREATE TABLE IF NOT EXISTS prompt_versions (
    id           BIGSERIAL PRIMARY KEY,           -- 主键ID
    agent_name   VARCHAR(50) NOT NULL,            -- Agent 名称
    version      VARCHAR(50) NOT NULL,            -- 版本号
    content_hash VARCHAR(64),                     -- Prompt 内容哈希（变更检测）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 创建时间
    is_active    BOOLEAN NOT NULL DEFAULT false   -- 是否当前生效版本
);
select * from prompt_versions;
-- ============================================================
-- 47. Evaluation
-- ============================================================

-- 评估用例（问题 + 期望 Agent/SQL/答案）
CREATE TABLE IF NOT EXISTS evaluation_cases (
    id                   BIGSERIAL PRIMARY KEY,           -- 主键ID
    question             TEXT NOT NULL,                   -- 测试问题
    expected_agents      JSONB,                           -- 期望调度的 Agent（{"required": [...]}，required 必须命中，多规划不扣分）
    expected_sql_pattern TEXT,                            -- 期望 SQL 模式（、分隔必命中表/关键词；rag:<部门> 为知识库检索；- 不判）
    expected_answer_key  TEXT,                            -- 期望答案关键词（、分隔，可注（m中n）；JUDGE: 前缀交 LLM 裁判）
    category             VARCHAR(30),                     -- 用例维度（routing/sql/fact/safety/rag）
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);
select * from evaluation_cases;
-- 评估运行批次
CREATE TABLE IF NOT EXISTS evaluation_runs (
    id          BIGSERIAL PRIMARY KEY,           -- 主键ID
    run_id      VARCHAR(100),                    -- 运行批次ID
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 开始时间
    finished_at TIMESTAMPTZ,                     -- 结束时间
    status      VARCHAR(20) NOT NULL DEFAULT 'running',  -- 状态
    notes       JSONB                            -- 批次元信息/汇总指标（mode、case_ids、token、费用、三维均分）
);
select * from evaluation_runs;
-- 评估得分（按 metric）
CREATE TABLE IF NOT EXISTS evaluation_scores (
    id        BIGSERIAL PRIMARY KEY,           -- 主键ID
    run_id    BIGINT NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,  -- 评估运行ID
    case_id   BIGINT NOT NULL REFERENCES evaluation_cases(id),  -- 用例ID
    metric    VARCHAR(50) NOT NULL,            -- 指标名（routing_accuracy/sql_accuracy/answer_accuracy）raw_capture 人工修改判定依据后可以用这个数据不查询llm二次判分
    score     NUMERIC(8,4),                    -- 得分
    detail    JSONB,                           -- 评分详情
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()       -- 创建时间
);
select * from evaluation_scores;
select * from evaluation_scores where metric = 'raw_capture' AND run_id = 2;
-- ============================================================
-- mart 层（设计文档 22、30 节）：Agent 优先查询层
-- ============================================================

-- 销售宽表（Agent 查销量/订单/GMV 的首选表）
CREATE TABLE IF NOT EXISTS mart_sales_daily (
    date          DATE NOT NULL,                  -- 日期（时间窗口过滤字段）
    country       VARCHAR(10) NOT NULL,           -- 国家
    brand_id      BIGINT REFERENCES brands(id),  -- 品牌 ID
    store_id      BIGINT NOT NULL REFERENCES stores(id),  -- 店铺ID
    sku_id        BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    orders        INT NOT NULL DEFAULT 0,         -- 订单数
    units         INT NOT NULL DEFAULT 0,         -- 销量（件）
    gmv           NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 成交金额（USD）
    refund_amount NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 退款金额
    PRIMARY KEY (date, store_id, sku_id)        -- 联合主键
);

-- 商品利润宽表（毛利/贡献利润/利润率）
CREATE TABLE IF NOT EXISTS mart_product_profit_daily (
    date               DATE NOT NULL,                  -- 日期
    country            VARCHAR(10) NOT NULL,           -- 国家
    brand_id           BIGINT REFERENCES brands(id),  -- 品牌 ID
    store_id           BIGINT NOT NULL REFERENCES stores(id),  -- 店铺ID
    sku_id             BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    revenue            NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 收入
    product_cost       NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 产品成本
    platform_fee       NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 平台费用
    advertising_cost   NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 广告成本
    logistics_cost     NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 物流成本
    refund_cost        NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 退款成本
    gross_profit       NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 毛利=revenue-product_cost
    contribution_profit NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 贡献利润
    profit_margin      NUMERIC(10,4),                  -- 利润率
    PRIMARY KEY (date, store_id, sku_id)        -- 联合主键
);
select  * from mart_product_profit_daily;
-- 广告效果宽表（花费/ROAS/CTR/CVR）
CREATE TABLE IF NOT EXISTS mart_ad_performance_daily (
    date         DATE NOT NULL,                   -- 日期
    platform     VARCHAR(50) NOT NULL,            -- 平台
    store_id     BIGINT NOT NULL REFERENCES stores(id),  -- 店铺ID
    campaign_id  BIGINT NOT NULL REFERENCES ad_campaigns(id),  -- 广告活动ID
    sku_id       BIGINT REFERENCES product_skus(id),  -- SKU ID
    impressions  BIGINT NOT NULL DEFAULT 0,       -- 曝光量
    clicks       INT NOT NULL DEFAULT 0,          -- 点击量
    spend        NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 广告花费（USD）
    conversions  INT NOT NULL DEFAULT 0,          -- 转化数
    revenue      NUMERIC(14,2) NOT NULL DEFAULT 0,  -- 广告收入
    ctr          NUMERIC(8,4),                   -- 点击率
    cvr          NUMERIC(8,4),                   -- 转化率
    cpc          NUMERIC(10,4),                  -- 单次点击成本
    roas         NUMERIC(10,4),                  -- ROAS
    PRIMARY KEY (date, campaign_id, sku_id)         -- 联合主键
);

-- 库存风险宽表（库存天数/预测需求/风险等级）
CREATE TABLE IF NOT EXISTS mart_inventory_risk (
    date           DATE NOT NULL,                  -- 日期
    warehouse_id   BIGINT NOT NULL REFERENCES warehouses(id),  -- 仓库ID
    sku_id         BIGINT NOT NULL REFERENCES product_skus(id),  -- SKU ID
    available_qty  INT NOT NULL DEFAULT 0,         -- 可售库存
    in_transit_qty INT NOT NULL DEFAULT 0,         -- 在途库存
    safety_stock   INT NOT NULL DEFAULT 0,         -- 安全库存
    stock_days     NUMERIC(10,2),                  -- 库存可售天数
    forecast_demand INT,                           -- 预测需求量
    risk_level     VARCHAR(20),                    -- 风险等级（high/medium/low）
    risk_reason    TEXT,                           -- 风险原因说明
    PRIMARY KEY (date, warehouse_id, sku_id)       -- 联合主键
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
