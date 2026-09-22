"""种子数据生成脚本（确定性、可重复执行）。

用法（项目根目录）：
    .venv\\Scripts\\python scripts\\seed_data.py

数据窗口：2026-06-18 ~ 2026-09-15（90 天），覆盖设计文档 23-32 节全部业务表 +
Agent 审计表 + 知识库 + 记忆 + 评估。

刻意埋点（供 Phase 1 Demo 使用）：
- SN-Q12-US（SweetNight 12寸Queen，美国亚马逊店）：最后 21 天销量骤降 ~65%，
  同时该 SKU 的品牌广告活动 spend 上涨 ~80% -> "GMV 下降 + 广告成本上升" 因果链
- NV-Q10-US（Novilla）：90 天平稳上行 -> 对照组
"""

from __future__ import annotations

import os
import random
from datetime import date, timedelta

import psycopg
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb

DSN = os.getenv(
    "SEED_DATABASE_URL",
    "postgresql://app_user:app_password@localhost:5432/sweetnight_agent",
)

# 需要清空重灌的表（TRUNCATE CASCADE 处理依赖）
TABLES = [
    "agent_results", "agent_interrupts", "agent_errors", "agent_tool_calls",
    "agent_steps", "agent_runs",
    # evaluation_cases/runs/scores 由 scripts/seed_evaluation_cases.py 独立维护，不在此清空重灌
    "prompt_versions", "agent_business_rule", "agent_metric_definition",
    "knowledge_embeddings", "knowledge_chunks", "knowledge_documents",
    "business_preferences", "user_preferences", "user_profiles",
    "mart_inventory_risk", "mart_ad_performance_daily",
    "mart_product_profit_daily", "mart_sales_daily",
    "return_reasons", "customer_feedback", "review_sentiments",
    "review_aspects", "reviews",
    "reconciliation_records", "fx_rates", "refunds", "platform_fees",
    "profit_daily", "cost_daily", "revenue_daily", "financial_transactions",
    "tracking_events", "logistics_cost", "logistics_orders",
    "outbound_shipments", "inbound_shipments", "inventory_daily",
    "inventory", "carriers", "warehouses",
    "ad_performance_daily", "ad_creatives", "ad_groups", "ad_campaigns",
    "sales_daily", "order_items", "orders",
    "product_development_projects", "product_lifecycle", "product_costs",
    "product_prices", "product_skus", "products", "product_categories",
    "store_accounts", "stores", "brands", "markets", "platforms",
    "role_permissions", "user_roles", "users", "departments",
]

START = date(2026, 6, 18)
END = date(2026, 9, 15)  # 90 天窗口（含首尾）
ANOMALY_START = date(2026, 8, 26)  # SN-Q12-US 异常窗口起点（最后 21 天）


def insert(cur, table: str, columns: list[str], rows: list[tuple], jsonb_cols: set[str] | None = None) -> None:
    if not rows:
        return
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    jsonb_cols = jsonb_cols or set()
    # 仅对声明为 JSONB 的列包装 dict/list；其余保持原样（如 vector 列）
    rows = [
        tuple(Jsonb(v) if col in jsonb_cols and isinstance(v, (dict, list)) else v
              for col, v in zip(columns, row))
        for row in rows
    ]
    cur.executemany(
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
        rows,
    )


def reset_sequences(cur, tables: list[str]) -> None:
    """把各表序列对齐到当前最大 id；无 id 列的表（复合主键）自动跳过。"""
    for t in tables:
        cur.execute(
            "SELECT pg_get_serial_sequence(table_schema || '.' || table_name, 'id') "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = 'id'",
            (t,),
        )
        row = cur.fetchone()
        seq = row[0] if row else None
        if seq:
            cur.execute(
                f"SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM {t}), 1))"
            )


def main() -> None:
    rng = random.Random(42)  # 固定种子，结果可复现
    with psycopg.connect(DSN, autocommit=True) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            print("[1/9] 清空旧数据 ...")
            cur.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")

            print("[2/9] 基础数据（部门/用户/平台/市场/品牌/商品/店铺）...")
            insert(cur, "departments", ["id", "name", "code", "description"], [
                (1, "运营部", "operation", "销售、流量、广告、转化"),
                (2, "物流部", "logistics", "库存、履约、物流成本与时效"),
                (3, "财务部", "finance", "收入、成本、利润核算"),
                (4, "产品部", "product", "市场研究、产品开发、竞品分析"),
                (5, "管理层", "management", "跨部门经营决策"),
            ])
            insert(cur, "users", ["id", "username", "display_name", "department_id", "email"], [
                (1, "zhangwei", "张伟", 1, "zhangwei@sweetnight.com"),
                (2, "lina", "李娜", 1, "lina@sweetnight.com"),
                (3, "wangqiang", "王强", 2, "wangqiang@sweetnight.com"),
                (4, "chenjing", "陈静", 3, "chenjing@sweetnight.com"),
                (5, "liuyang", "刘洋", 4, "liuyang@sweetnight.com"),
                (6, "zhaomin", "赵敏", 4, "zhaomin@sweetnight.com"),
                (7, "huanglei", "黄磊", 5, "huanglei@sweetnight.com"),
                (8, "sunqi", "孙琪", 3, "sunqi@sweetnight.com"),
            ])
            insert(cur, "user_roles", ["user_id", "role_name"], [
                (1, "admin"), (3, "admin"), (5, "admin"),
                (2, "analyst"), (4, "analyst"), (6, "analyst"), (8, "analyst"),
                (7, "manager"),
            ])
            insert(cur, "role_permissions", ["role_name", "permission"], [
                ("admin", "query:all"), ("admin", "report:all"),
                ("analyst", "query:department"),
                ("manager", "query:all"), ("manager", "report:all"),
            ])

            insert(cur, "platforms", ["id", "code", "name"], [
                (1, "amazon", "Amazon"),
                (2, "tiktok_shop", "TikTok Shop"),
                (3, "walmart", "Walmart"),
                (4, "shopify", "Shopify"),
            ])
            insert(cur, "markets", ["id", "code", "name", "currency"], [
                (1, "US", "美国", "USD"),
                (2, "CA", "加拿大", "CAD"),
                (3, "UK", "英国", "GBP"),
                (4, "DE", "德国", "EUR"),
                (5, "AU", "澳大利亚", "AUD"),
            ])
            insert(cur, "brands", ["id", "name", "description"], [
                (1, "SweetNight", "美国市场主品牌，床垫/床架/枕头"),
                (2, "Novilla", "高性价比床垫品牌"),
                (3, "Avenco", "中高端床垫品牌"),
                (4, "甜秘密", "国内品牌，出口美国"),
            ])
            insert(cur, "product_categories", ["id", "parent_id", "name", "code"], [
                (1, None, "床垫", "mattress"),
                (2, None, "床架", "bed_frame"),
                (3, None, "枕头", "pillow"),
                (4, None, "床垫配件", "mattress_accessory"),
            ])
            insert(cur, "products", ["id", "brand_id", "category_id", "product_name", "product_type", "launch_date"], [
                (1, 1, 1, "SweetNight 12 Inch Queen Mattress", "12in-queen", date(2024, 3, 15)),
                (2, 1, 1, "SweetNight 12 Inch King Mattress", "12in-king", date(2024, 3, 15)),
                (3, 1, 1, "SweetNight 10 Inch Queen Mattress", "10in-queen", date(2024, 8, 1)),
                (4, 1, 1, "SweetNight 10 Inch Full Mattress", "10in-full", date(2024, 11, 20)),
                (5, 2, 1, "Novilla 10 Inch Queen Mattress", "10in-queen", date(2024, 6, 10)),
                (6, 3, 1, "Avenco 12 Inch Queen Mattress", "12in-queen", date(2024, 4, 25)),
                (7, 1, 2, "SweetNight Queen Platform Bed Frame", "queen-frame", date(2025, 1, 15)),
                (8, 1, 3, "SweetNight Memory Foam Pillow 2 Pack", "pillow-2pk", date(2025, 3, 1)),
                (9, 4, 1, "甜秘密护脊床垫 1.8m", "180cm-queen", date(2024, 9, 1)),
                (10, 1, 4, "SweetNight 3 Inch Gel Memory Foam Topper", "topper-3in", date(2025, 6, 1)),
            ])
            # sku: (id, product_id, sku_code, country, price, cost, weight)
            sku_rows = [
                (1, 1, "SN-Q12-US", "US", 259.99, 88.50, 27.2),
                (2, 1, "SN-Q12-CA", "CA", 319.99, 88.50, 27.2),
                (3, 2, "SN-K12-US", "US", 329.99, 112.30, 35.0),
                (4, 3, "SN-Q10-US", "US", 199.99, 72.00, 22.5),
                (5, 4, "SN-F10-US", "US", 179.99, 65.50, 20.1),
                (6, 5, "NV-Q10-US", "US", 209.99, 76.80, 23.0),
                (7, 6, "AV-Q12-US", "US", 279.99, 95.20, 28.4),
                (8, 1, "SN-Q12-UK", "UK", 229.99, 88.50, 27.2),
                (9, 7, "SN-BFQ-US", "US", 149.99, 52.00, 18.6),
                (10, 8, "SN-PL2-US", "US", 59.99, 18.40, 2.6),
                (11, 10, "SN-TP3-US", "US", 89.99, 30.20, 7.8),
                (12, 9, "TM-Q18-US", "US", 299.99, 105.00, 30.0),
            ]
            insert(cur, "product_skus", ["id", "product_id", "sku_code", "country_code", "sale_price", "cost", "weight"], sku_rows)
            insert(cur, "product_prices", ["sku_id", "country_code", "currency", "price", "effective_from"], [
                (s, c, c, p, START) for s, _, _, c, p, _, _ in sku_rows
            ])
            insert(cur, "product_costs", ["sku_id", "cost_type", "amount", "currency", "effective_from"], [
                (s, "product", c, "USD", START) for s, _, _, _, _, c, _ in sku_rows
            ])
            insert(cur, "product_lifecycle", ["sku_id", "stage", "start_date"], [
                (1, "mature", date(2025, 1, 1)),
                (2, "mature", date(2025, 1, 1)),
                (3, "mature", date(2025, 1, 1)),
                (4, "growth", date(2025, 1, 1)),
                (6, "growth", date(2025, 1, 1)),
                (9, "growth", date(2025, 6, 1)),
            ])

            insert(cur, "stores", ["id", "platform_id", "brand_id", "country_code", "store_name", "store_type"], [
                (1, 1, 1, "US", "SweetNight Official Amazon US", "marketplace"),
                (2, 2, 1, "US", "SweetNight TikTok Shop US", "marketplace"),
                (3, 1, 1, "CA", "SweetNight Amazon CA", "marketplace"),
                (4, 1, 1, "UK", "SweetNight Amazon UK", "marketplace"),
                (5, 1, 2, "US", "Novilla Amazon US", "marketplace"),
                (6, 1, 3, "US", "Avenco Amazon US", "marketplace"),
                (7, 3, 1, "US", "SweetNight Walmart US", "marketplace"),
                (8, 4, 1, "US", "SweetNight Official Site US", "d2c"),
            ])
            insert(cur, "store_accounts", ["store_id", "account_name"], [
                (1, "sweetnight-amazon-us"), (2, "sweetnight-ttk-us"),
                (3, "sweetnight-amazon-ca"), (4, "sweetnight-amazon-uk"),
                (5, "novilla-amazon-us"), (6, "avenco-amazon-us"),
                (7, "sweetnight-walmart-us"), (8, "sweetnight-shopify-us"),
            ])

            insert(cur, "warehouses", ["id", "code", "name", "country_code", "region"], [
                (1, "US-EAST", "美国东部仓（NJ）", "US", "East"),
                (2, "US-WEST", "美国西部仓（CA）", "US", "West"),
                (3, "UK-LON", "英国伦敦仓", "UK", "Europe"),
            ])
            insert(cur, "carriers", ["id", "code", "name", "service_level"], [
                (1, "UPS", "UPS", "ground"),
                (2, "FEDEX", "FedEx", "ground"),
                (3, "USPS", "USPS", "parcel"),
                (4, "DHL", "DHL", "express"),
            ])

            print("[3/9] 广告层级（campaign / group / creative）...")
            insert(cur, "ad_campaigns", ["id", "store_id", "platform", "campaign_name", "objective", "daily_budget", "start_date"], [
                (1, 1, "amazon", "SN US Brand Defense", "brand", 400.00, START),
                (2, 1, "amazon", "SN US Performance Queen", "conversion", 450.00, START),
                (3, 2, "tiktok_shop", "SN TTK US Prospecting", "traffic", 280.00, START),
                (4, 3, "amazon", "SN CA Prospecting", "conversion", 150.00, START),
            ])
            insert(cur, "ad_groups", ["id", "campaign_id", "ad_group_name"], [
                (1, 1, "SN-Q12 Brand Exact"), (2, 1, "SN-Q12 Competitor"),
                (3, 2, "Queen Broad"), (4, 2, "King Broad"),
                (5, 3, "TTK Top Funnel"), (6, 4, "CA Queen"),
            ])
            insert(cur, "ad_creatives", ["id", "ad_group_id", "creative_name", "creative_type"], [
                (1, 1, "Q12 Hero Image", "image"), (2, 1, "Q12 Video 15s", "video"),
                (3, 2, "Competitor Keyword", "image"), (4, 3, "Queen Carousel", "image"),
                (5, 4, "King Carousel", "image"), (6, 5, "TTK UGC Review", "video"),
                (7, 6, "CA Queen Image", "image"),
            ])

            print("[4/9] 生成 90 天销售与订单数据 ...")
            # 价格 / 成本字典
            price = {s: p for s, _, _, _, p, _, _ in sku_rows}
            cost = {s: c for s, _, _, _, _, c, _ in sku_rows}
            # 销售流：(store_id, sku_id, base_units, growth_frac, anomaly 标志)
            streams = [
                (1, 1, 22.0, 0.0, True),    # SN-Q12-US 亚马逊主店（异常：末 21 天骤降）
                (2, 1, 8.0, 0.0, False),    # SN-Q12-US TikTok
                (7, 1, 5.0, 0.0, False),    # SN-Q12-US Walmart
                (1, 3, 10.0, 0.35, False),  # SN-K12-US 上行
                (1, 4, 14.0, 0.0, False),   # SN-Q10-US
                (1, 5, 6.0, -0.15, False),  # SN-F10-US 略降
                (5, 6, 9.0, 0.45, False),   # NV-Q10-US 上行（对照组）
                (6, 7, 5.0, 0.0, False),    # AV-Q12-US
                (1, 9, 4.0, 0.0, False),    # SN-BFQ-US 床架
                (7, 10, 6.0, 0.1, False),   # SN-PL2-US 枕头
                (8, 11, 5.0, 0.2, False),   # SN-TP3-US 床垫配件
                (8, 12, 3.0, 0.0, False),   # TM-Q18-US
                (3, 2, 3.0, 0.0, False),    # CA 市场
                (4, 8, 2.0, 0.0, False),    # UK 市场
            ]
            days = (END - START).days + 1

            # sku_id -> brand_id（用于 mart 层品牌过滤）
            sku_brand = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 2, 7: 3, 8: 1, 9: 1, 10: 1, 11: 1, 12: 4}

            sales_rows: list[tuple] = []
            order_rows: list[tuple] = []
            item_rows: list[tuple] = []
            order_seq = 0
            for i in range(days):
                d = START + timedelta(days=i)
                weekend = d.weekday() >= 5
                for store_id, sku_id, base, growth, anomaly in streams:
                    trend = 1.0 + growth * (i / max(days - 1, 1))
                    # 周末加成
                    wd = 1.25 if weekend else 1.0
                    units = base * trend * wd * rng.uniform(0.92, 1.10)
                    if anomaly and d >= ANOMALY_START:
                        # 末 21 天加速下滑：最终跌至基线的 30%（前 1/3 温和，后 2/3 陡峭）
                        t = (d - ANOMALY_START).days / 20.0
                        units *= 1.0 - 0.7 * min(t ** 0.6, 1.0)
                    units = max(int(round(units)), 0)
                    if units == 0:
                        continue
                    gmv = round(units * price[sku_id], 2)
                    refund_amt = round(gmv * rng.uniform(0.01, 0.045), 2)
                    n_orders = max(1, int(round(units * rng.uniform(0.8, 0.98))))
                    brand_id = sku_brand[sku_id]
                    sales_rows.append((d, "US" if store_id != 4 else "UK", brand_id, store_id, sku_id, n_orders, units, gmv, refund_amt))
                    # 生成该组合的订单样本（最多 6 单/天）
                    for _ in range(min(n_orders, 6)):
                        qty = rng.choice([1, 1, 1, 2])
                        amt = round(price[sku_id] * qty, 2)
                        order_seq += 1
                        platform_order_id = f"ORD-{d:%Y%m%d}-{store_id}-{sku_id}-{order_seq}"
                        order_rows.append((platform_order_id, store_id, d, amt))
                        item_rows.append((sku_id, qty, amt))
            # 补全 order/order_items 的 id
            order_full: list[tuple] = []
            item_full: list[tuple] = []
            for i, (pid, sid, d, amt) in enumerate(order_rows, start=1):
                order_full.append((i, pid, sid, "UK" if sid == 4 else "US", d, amt))
            for i, (sku_id, qty, amt) in enumerate(item_rows, start=1):
                item_full.append((i, i, sku_id, qty, amt))
            insert(cur, "sales_daily", ["date", "country", "brand_id", "store_id", "sku_id", "orders", "units", "gmv", "refund_amount"], sales_rows)
            insert(cur, "mart_sales_daily", ["date", "country", "brand_id", "store_id", "sku_id", "orders", "units", "gmv", "refund_amount"], sales_rows)
            insert(cur, "orders", ["id", "platform_order_id", "store_id", "country_code", "order_date", "order_amount"], order_full)
            insert(cur, "order_items", ["id", "order_id", "sku_id", "quantity", "sale_amount"], item_full)

            print("[5/9] 广告表现数据（90 天）...")
            # campaign: (campaign_id, store_id, sku_id, base_spend, spend_growth, ctr, cpc, cvr)
            campaigns = [
                (1, 1, 1, 380.0, 0.85, 0.0042, 0.82, 0.095),   # SN-Q12 品牌防御：末 21 天 spend +85%
                (2, 1, 3, 240.0, 0.0, 0.0038, 0.75, 0.085),
                (2, 1, 4, 200.0, 0.0, 0.0040, 0.70, 0.090),
                (3, 2, 1, 260.0, 0.0, 0.0120, 0.55, 0.110),
                (4, 3, 2, 130.0, 0.0, 0.0035, 0.60, 0.080),
            ]
            ad_rows: list[tuple] = []
            for i in range(days):
                d = START + timedelta(days=i)
                for cid, store_id, sku_id, base_spend, growth, ctr, cpc, cvr in campaigns:
                    spend = base_spend * rng.uniform(0.94, 1.08)
                    if cid == 1 and d >= ANOMALY_START:
                        t = (d - ANOMALY_START).days / 20.0
                        spend *= 1.0 + growth * min(t, 1.0)
                    clicks = int(round(spend / cpc))
                    impressions = int(round(clicks / ctr))
                    conversions = int(round(clicks * cvr * rng.uniform(0.9, 1.1)))
                    rev = round(spend * rng.uniform(2.6, 3.4), 2)
                    ad_rows.append((
                        d, "amazon" if cid != 3 else "tiktok_shop", store_id, cid, sku_id,
                        impressions, clicks, round(spend, 2), conversions, rev,
                        round(ctr, 4), round(cvr, 4), round(cpc, 4), round(rev / spend, 4),
                    ))
            insert(cur, "ad_performance_daily", ["date", "platform", "store_id", "campaign_id", "sku_id", "impressions", "clicks", "spend", "conversions", "revenue", "ctr", "cvr", "cpc", "roas"], ad_rows)
            insert(cur, "mart_ad_performance_daily", ["date", "platform", "store_id", "campaign_id", "sku_id", "impressions", "clicks", "spend", "conversions", "revenue", "ctr", "cvr", "cpc", "roas"], ad_rows)

            print("[6/9] 财务宽表（mart_product_profit_daily / profit_daily / 成本日表 / 汇率）...")
            # 每单位物流成本（按品类）：床垫类 ~20，床架 12，枕头 4，配件 7
            unit_logistics = {1: 20.5, 2: 20.5, 3: 20.5, 4: 20.5, 5: 20.5, 6: 20.5,
                              7: 20.5, 8: 20.5, 9: 12.0, 10: 4.0, 11: 7.0, 12: 20.5}
            profit_rows: list[tuple] = []
            rev_daily: dict[tuple, float] = {}
            cost_daily_rows: list[tuple] = []
            for (d, country, brand_id, store_id, sku_id, orders, units, gmv, refund_amt) in sales_rows:
                revenue = gmv
                platform_fee_rate = 0.06 if store_id == 8 else 0.15
                product_cost = round(units * cost[sku_id], 2)
                platform_fee = round(revenue * platform_fee_rate, 2)
                ad_spend = sum(r[7] for r in ad_rows if r[0] == d and r[3] in (1, 2, 3, 4) and r[4] == sku_id and r[2] == store_id)
                logi_cost = round(units * unit_logistics[sku_id], 2)
                gross = round(revenue - product_cost - platform_fee, 2)
                contribution = round(gross - ad_spend - logi_cost - refund_amt, 2)
                margin = round(contribution / revenue, 4) if revenue else 0
                profit_rows.append((d, country, brand_id, store_id, sku_id, revenue, product_cost, platform_fee, round(ad_spend, 2), logi_cost, refund_amt, gross, contribution, margin))
                rev_daily[(d, store_id)] = rev_daily.get((d, store_id), 0.0) + revenue
            insert(cur, "mart_product_profit_daily", ["date", "country", "brand_id", "store_id", "sku_id", "revenue", "product_cost", "platform_fee", "advertising_cost", "logistics_cost", "refund_cost", "gross_profit", "contribution_profit", "profit_margin"], profit_rows)
            insert(cur, "profit_daily", ["date", "store_id", "sku_id", "revenue", "product_cost", "platform_fee", "advertising_cost", "logistics_cost", "refund_cost", "gross_profit", "contribution_profit", "profit_margin"], [
                (r[0], r[3], r[4], *r[5:]) for r in profit_rows
            ])

            for (d, store_id), rev in rev_daily.items():
                cost_daily_rows.append((d, store_id, "platform_fee", round(rev * (0.06 if store_id == 8 else 0.15), 2)))
                cost_daily_rows.append((d, store_id, "refund", round(rev * 0.025, 2)))
            insert(cur, "revenue_daily", ["date", "store_id", "country", "revenue"], [
                (d, s, "US" if s != 4 else "UK", round(v, 2)) for (d, s), v in sorted(rev_daily.items())
            ])
            insert(cur, "cost_daily", ["date", "store_id", "cost_type", "amount"], cost_daily_rows)

            # 汇率：90 天
            fx_base = {"USDCAD": 1.36, "USDGBP": 0.79, "USDEUR": 0.92, "USDAUD": 1.51, "USDCNY": 7.20}
            fx_rows = []
            for i in range(days):
                d = START + timedelta(days=i)
                for pair, base in fx_base.items():
                    fx_rows.append((pair, d, round(base * rng.uniform(0.995, 1.005), 6)))
            insert(cur, "fx_rates", ["currency_pair", "rate_date", "rate"], fx_rows)

            insert(cur, "platform_fees", ["order_id", "store_id", "fee_type", "amount"], [
                (i, o[2], "referral", round(o[5] * (0.06 if o[2] == 8 else 0.15), 2))
                for i, o in enumerate(order_full, start=1)
            ])

            print("[7/9] 评论 / VOC 与库存物流 ...")
            review_templates_pos = [
                "Very comfortable mattress, sleep much better now.",
                "Great value for the price, arrived fast and well packaged.",
                "Firm but supportive, exactly as described.",
                "My back feels great after a week of use.",
            ]
            review_templates_neg = [
                "Sagging in the middle after 2 months, not happy.",
                "Took long to expand and has a strong smell.",
                "Too soft for my taste, sinking in the middle.",
                "Started making noise, quality seems inconsistent.",
            ]
            reviews_rows = []
            aspects_rows = []
            senti_rows = []
            rid = 0
            for (d, _, _, store_id, sku_id, orders, units, gmv, _) in sales_rows:
                if sku_id not in (1, 3, 4, 6, 11):
                    continue
                if rng.random() < 0.03:  # 约 3% 的日组合产生评论
                    rid += 1
                    neg = d >= ANOMALY_START and sku_id == 1 and rng.random() < 0.5
                    rating = rng.choices([5, 4, 3, 2, 1], weights=[45, 30, 12, 8, 5] if not neg else [15, 20, 25, 25, 15])[0]
                    text = rng.choice(review_templates_neg if rating <= 2 else review_templates_pos)
                    platform_name = {1: "amazon", 2: "tiktok_shop", 3: "amazon", 4: "amazon", 5: "amazon", 6: "amazon", 7: "walmart", 8: "shopify"}[store_id]
                    reviews_rows.append((platform_name, sku_id, "US", rating, text, d))
                    for aspect, sentiment in [
                        ("comfort", "negative" if rating <= 2 else "positive"),
                        ("delivery", "positive"),
                        ("quality", "negative" if rating <= 2 else "positive"),
                        ("price", "positive"),
                    ]:
                        aspects_rows.append((rid, aspect, sentiment, round(rating * 0.2 + rng.uniform(-0.2, 0.2), 2)))
                    senti_rows.append((rid, "overall", round(rating / 5.0, 4), 0.9))
            insert(cur, "reviews", ["platform", "sku_id", "country", "rating", "review_text", "review_date"], reviews_rows)
            insert(cur, "review_aspects", ["review_id", "aspect", "sentiment", "score"], aspects_rows)
            insert(cur, "review_sentiments", ["review_id", "sentiment_type", "score", "confidence"], senti_rows)
            insert(cur, "return_reasons", ["order_id", "sku_id", "reason_code", "reason_detail", "return_date"], [
                (i, item_full[i - 1][2], "defective", "customer reported sagging", END - timedelta(days=rng.randint(0, 25)))
                for i in rng.sample(range(1, len(order_full) + 1), min(12, len(order_full)))
            ])

            # 库存快照 + 30 天库存日表 + 风险表
            inv_snapshot = [
                (1, 1, 320, 40, 500, 150),   # SN-Q12-US US-EAST：在途 500，安全库存 150 -> 库存天数偏低
                (2, 1, 180, 20, 0, 90),
                (1, 3, 210, 25, 300, 90),
                (2, 3, 140, 15, 0, 70),
                (1, 4, 180, 20, 200, 100),
                (2, 6, 260, 30, 0, 120),
                (1, 9, 120, 10, 0, 60),
                (1, 10, 400, 30, 0, 150),
                (1, 11, 260, 20, 0, 100),
            ]
            insert(cur, "inventory", ["warehouse_id", "sku_id", "available_qty", "reserved_qty", "in_transit_qty", "safety_stock"], inv_snapshot)
            inv_daily_rows = []
            inv_risk_rows = []
            last_30 = 30
            for k in range(last_30):
                d = END - timedelta(days=last_30 - 1 - k)
                for wh_id, sku_id, avail, resv, transit, safety in inv_snapshot:
                    # 过去 30 天销量（用于库存天数）
                    daily_units = sum(u for (dd, _, _, st, sk, _, u, _, _) in sales_rows if dd == d and st == wh_id and sk == sku_id) or 1
                    avail_now = max(avail - k * rng.randint(1, 3), 10)
                    stock_days = round(avail_now / max(daily_units, 1), 1)
                    inv_daily_rows.append((d, wh_id, sku_id, avail_now, resv, transit, safety, stock_days))
                    if sku_id == 1 and wh_id == 1:
                        risk = "high" if stock_days < 12 else "medium"
                    elif sku_id == 6:
                        risk = "low"
                    else:
                        risk = "low" if stock_days > 15 else "medium"
                    inv_risk_rows.append((d, wh_id, sku_id, avail_now, transit, safety, stock_days, int(daily_units * 15), risk,
                                          "库存天数低于安全阈值" if risk == "high" else None))
            insert(cur, "inventory_daily", ["date", "warehouse_id", "sku_id", "available_qty", "reserved_qty", "in_transit_qty", "safety_stock", "stock_days"], inv_daily_rows)
            insert(cur, "mart_inventory_risk", ["date", "warehouse_id", "sku_id", "available_qty", "in_transit_qty", "safety_stock", "stock_days", "forecast_demand", "risk_level", "risk_reason"], inv_risk_rows)
            insert(cur, "inbound_shipments", ["warehouse_id", "sku_id", "quantity", "eta_date", "status"], [
                (1, 1, 500, END + timedelta(days=20), "in_transit"),
                (1, 3, 300, END + timedelta(days=12), "in_transit"),
            ])

            # 物流订单 / 成本 / 追踪事件（抽样订单）
            logi_rows = []
            logi_cost_rows = []
            track_rows = []
            logi_id = 0
            sample_orders = rng.sample(order_full, min(800, len(order_full)))
            for oid, _, store_id, _, d, amt in sample_orders:
                logi_id += 1
                carrier = rng.choice([1, 2, 3, 4])
                ship = d + timedelta(days=rng.randint(1, 3))
                deliver = ship + timedelta(days=rng.randint(3, 9))
                logi_rows.append((oid, carrier, f"TRK{logi_id:08d}", 1 if store_id in (1, 2, 7) else 3, ship, deliver, "delivered"))
                qty = item_full[oid - 1][3]
                sku_id = item_full[oid - 1][2]
                logi_cost_rows.append((oid, carrier, "shipping", round(qty * unit_logistics[sku_id], 2)))
                track_rows.append((logi_id, "DELIVERED", "Delivered", deliver, "US"))
            insert(cur, "logistics_orders", ["order_id", "carrier_id", "tracking_number", "warehouse_id", "ship_date", "delivery_date", "status"], logi_rows)
            insert(cur, "logistics_cost", ["order_id", "carrier_id", "cost_type", "amount"], logi_cost_rows)
            insert(cur, "tracking_events", ["logistics_order_id", "event_code", "event_name", "event_date", "location"], track_rows)
            insert(cur, "outbound_shipments", ["order_id", "warehouse_id", "sku_id", "quantity", "ship_date", "status"], [
                (oid, 1 if store_id in (1, 2, 7) else 3, item_full[oid - 1][2], item_full[oid - 1][3], d + timedelta(days=1), "shipped")
                for oid, _, store_id, _, d, _ in sample_orders
            ])

            print("[8/9] Agent 审计 / 记忆 / 知识库 / 元数据 / 评估 ...")
            insert(cur, "agent_runs", ["id", "thread_id", "user_id", "question", "status", "started_at", "finished_at", "total_tokens", "total_cost"], [
                (1, "thr-001", 1, "分析美国市场过去90天SweetNight床垫GMV、订单、销量、转化率变化，并找出异常SKU", "completed", "2026-09-15T09:00:00+08:00", "2026-09-15T09:01:30+08:00", 18200, 0.42),
                (2, "thr-002", 4, "为什么最近美国市场利润下降", "completed", "2026-09-16T10:00:00+08:00", "2026-09-16T10:03:00+08:00", 31500, 0.78),
                (3, "thr-003", 2, "美国市场广告ROI为什么下降", "interrupted", "2026-09-16T14:00:00+08:00", None, 6200, 0.15),
            ])
            insert(cur, "agent_steps", ["run_id", "agent_name", "node_name", "step_index", "input_summary", "output_summary", "started_at", "finished_at", "status"], [
                (1, "manager", "understand", 1, "识别跨部门复杂问题", "任务DAG: operation->decision", "2026-09-15T09:00:01+08:00", "2026-09-15T09:00:05+08:00", "success"),
                (1, "operation", "loop", 2, "查询90天GMV/订单/销量", "发现SN-Q12-US末21天下滑65%", "2026-09-15T09:00:10+08:00", "2026-09-15T09:01:00+08:00", "success"),
                (1, "decision", "finalize", 3, "汇总运营结果", "输出最终报告", "2026-09-15T09:01:05+08:00", "2026-09-15T09:01:30+08:00", "success"),
            ])
            insert(cur, "agent_tool_calls", ["run_id", "agent_name", "tool_name", "arguments", "result_summary", "duration_ms", "status"],
                   [
                (1, "operation", "list_tables_tool", {"prefix": "mart"}, "返回 mart_* 表列表", 45, "success"),
                (1, "operation", "execute_readonly_sql", {"sql": "SELECT date, SUM(gmv) FROM mart_sales_daily WHERE date >= '2026-06-18' GROUP BY date"}, "90 行，GMV 趋势", 312, "success"),
                (1, "operation", "execute_readonly_sql", {"sql": "SELECT sku_id, SUM(units) FROM mart_sales_daily GROUP BY sku_id ORDER BY 2 DESC"}, "SKU 排行，SN-Q12-US 异常", 286, "success"),
                (3, "operation", "execute_readonly_sql", {"sql": "SELECT * FROM mart_ad_performance_daily"}, "缺少时间范围", 200, "failed"),
            ], jsonb_cols={"arguments"})
            insert(cur, "agent_errors", ["run_id", "agent_name", "node_name", "error_type", "error_message", "retry_count"], [
                (3, "operation", "sql_execute", "SQLValidationError", "必须包含 LIMIT 子句", 1),
            ])
            insert(cur, "agent_interrupts", ["run_id", "thread_id", "agent_name", "interrupt_type", "payload", "status", "created_at"], [
                (3, "thr-003", "operation", "missing_param", {"missing": ["time_range", "market"]}, "pending", "2026-09-16T14:00:30+08:00"),
            ], jsonb_cols={"payload"})
            insert(cur, "agent_results", ["run_id", "agent_name", "result_type", "result"], [
                (1, "operation", "department_result", {"key_findings": ["SN-Q12-US GMV 末21天下滑65%"], "confidence": 0.9}),
                (1, "decision", "final_report", {"summary": "美国市场 GMV 整体平稳，异常 SKU 为 SN-Q12-US", "confidence": 0.86}),
            ], jsonb_cols={"result"})

            insert(cur, "user_profiles", ["user_id", "key", "value"], [
                (1, "role", "运营负责人"), (1, "focus", "美国市场"),
                (4, "role", "财务负责人"),
            ])
            insert(cur, "user_preferences", ["user_id", "key", "value"], [
                (1, "default_market", "US"), (1, "default_currency", "USD"),
                (1, "report_format", "markdown"), (1, "default_time_range", "90"),
            ])
            insert(cur, "business_preferences", ["key", "value", "scope"], [
                ("default_market", "US", "global"),
                ("safety_stock_days", "15", "global"),
                ("ad_roas_redline", "1.5", "global"),
            ])

            # 知识库（向量为 1536 维确定性随机）
            dim = 1536
            def vec() -> list[float]:
                v = [rng.uniform(-1, 1) for _ in range(dim)]
                norm = sum(x * x for x in v) ** 0.5
                return [x / norm for x in v]

            insert(cur, "knowledge_documents", ["id", "title", "source_type", "department", "brand", "market", "version", "status"], [
                (1, "2026年美国床垫市场趋势报告", "report", "product", None, "US", "2026-01", "active"),
                (2, "甜秘密新品开发SOP", "SOP", "product", None, None, "v3.2", "active"),
                (3, "SweetNight 12寸Queen床垫产品规格书", "product_spec", "product", "SweetNight", "US", "v2.1", "active"),
                (4, "跨境电商物流SLA与仓储规则", "SOP", "logistics", None, "US", "v1.8", "active"),
            ])
            kb_content = [
                (1, 0, "2026年美国床垫市场规模预计增长6.2%，Queen尺寸占线上销量40%以上，记忆棉是主流材质。消费者对支撑性和耐久性关注度上升。"),
                (1, 1, "主要竞品包括 Zinus、Tuft & Needle、Casper。价格带 $150-$350 竞争最激烈，差异化集中在材质、厚度与试睡政策。"),
                (2, 0, "新品开发流程：市场调研 -> 立项评审 -> 原型打样 -> 用户测试 -> 成本核算 -> 量产评审 -> 上市。每个环节需部门会签。"),
                (2, 1, "立项评审必须包含：目标市场、目标价格带、目标毛利率（不低于35%）、竞品对比、风险清单。"),
                (3, 0, "SweetNight 12 Inch Queen：三层记忆棉结构，密度 3.5lb，硬度 6/10，10年质保，重量 60lbs，卷压包装。"),
                (3, 1, "售后客诉常见问题：塌陷（占比 38%）、异味（22%）、边缘支撑不足（15%）。"),
                (4, 0, "美国市场标准履约 SLA：订单 48h 内出库，地面运输 3-7 天，时效达成率目标 95%。"),
                (4, 1, "库存规则：安全库存 = 15 天预测销量；库存天数低于 12 天触发补货预警；低于 7 天触发缺货风险。"),
            ]
            for doc_id, idx, content in kb_content:
                e = vec()
                insert(cur, "knowledge_chunks", ["document_id", "chunk_index", "content", "metadata", "embedding"], [
                    (doc_id, idx, content, {"department": "product" if doc_id in (1, 2, 3) else "logistics", "document_type": "SOP" if doc_id in (2, 4) else "report"}, e),
                ], jsonb_cols={"metadata"})
            chunk_ids = list(range(1, len(kb_content) + 1))
            insert(cur, "knowledge_embeddings", ["chunk_id", "model", "dimension", "embedding"], [
                (c, "text-embedding-3-small", dim, vec()) for c in chunk_ids
            ])

            insert(cur, "agent_metric_definition", ["metric_name", "definition", "formula", "unit", "department"], [
                ("GMV", "成交总额", "SUM(units * sale_price)", "USD", "operation"),
                ("订单量", "订单数量", "COUNT(orders)", "单", "operation"),
                ("销量", "售出件数", "SUM(units)", "件", "operation"),
                ("CVR", "转化率", "conversions / clicks", "%", "operation"),
                ("CTR", "点击率", "clicks / impressions", "%", "operation"),
                ("CPC", "单次点击成本", "spend / clicks", "USD", "operation"),
                ("ROAS", "广告支出回报", "revenue / spend", "倍", "operation"),
                ("贡献毛利", "扣除直接成本后毛利", "revenue - product_cost - platform_fee", "USD", "finance"),
                ("贡献利润率", "贡献毛利占比", "contribution_profit / revenue", "%", "finance"),
                ("库存天数", "现有库存可售天数", "available_qty / daily_sales", "天", "logistics"),
                ("缺货率", "缺货订单占比", "stockout_orders / total_orders", "%", "logistics"),
            ])
            insert(cur, "agent_business_rule", ["rule_name", "rule_type", "description", "department"], [
                ("安全库存天数", "threshold", "安全库存 = 15 天预测销量", "logistics"),
                ("广告ROI红线", "threshold", "ROAS 低于 1.5 时建议暂停低效广告组", "operation"),
                ("退款率预警", "threshold", "退款率超过 5% 触发预警", "finance"),
                ("库存风险等级", "classification", "库存天数 <12 高风险，<15 中风险", "logistics"),
            ])
            insert(cur, "prompt_versions", ["agent_name", "version", "content_hash", "is_active"], [
                ("manager", "v1", "a" * 64, True),
                ("operation", "v1", "b" * 64, True),
                ("decision", "v1", "c" * 64, True),
            ])
            # 评估用例/运行/得分由 scripts/seed_evaluation_cases.py 独立维护（20 条正式用例），此处不再灌占位数据
            insert(cur, "financial_transactions", ["store_id", "transaction_date", "transaction_type", "amount", "currency", "description"], [
                (1, END, "platform_payout", 48210.5, "USD", "9月中旬平台结算"),
                (3, END, "platform_payout", 8120.3, "CAD", "9月中旬平台结算"),
            ])
            insert(cur, "reconciliation_records", ["period_start", "period_end", "store_id", "platform_revenue", "system_revenue", "difference", "status"], [
                (date(2026, 8, 1), date(2026, 8, 31), 1, 152300.0, 151900.0, 400.0, "completed"),
                (date(2026, 9, 1), END, 1, 124800.0, 124600.0, 200.0, "pending"),
            ])
            insert(cur, "customer_feedback", ["source", "content", "sentiment"], [
                ("客服工单", "客户反馈 SN-Q12 使用两个月后中部塌陷", "negative"),
                ("社媒", "用户晒单：SweetNight 12寸很舒服", "positive"),
                ("客服工单", "物流延迟导致客户不满", "neutral"),
            ])
            insert(cur, "product_development_projects", ["id", "name", "brand_id", "status", "stage", "owner_user_id", "planned_launch_date", "description"], [
                (1, "2026 Q4 混合记忆棉床垫（US）", 1, "active", "prototype", 5, date(2026, 11, 15), "针对塌陷客诉开发高密度混合棉"),
                (2, "Novilla 可折叠床垫", 2, "draft", "research", 6, date(2027, 3, 1), "面向公寓人群"),
            ])

            reset_sequences(cur, TABLES)
            print("[9/9] 序列对齐完成")

    # 汇总
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            print("\n===== 数据汇总 =====")
            for t in ["departments", "users", "platforms", "markets", "brands", "products",
                      "product_skus", "stores", "orders", "order_items", "sales_daily",
                      "ad_campaigns", "ad_performance_daily", "mart_sales_daily",
                      "mart_product_profit_daily", "mart_ad_performance_daily",
                      "inventory_daily", "mart_inventory_risk", "logistics_orders",
                      "reviews", "fx_rates", "agent_runs", "knowledge_documents",
                      "knowledge_chunks", "agent_metric_definition", "evaluation_cases"]:
                cur.execute(f"SELECT count(*) FROM {t}")
                print(f"  {t:<28} {cur.fetchone()[0]:>8}")


if __name__ == "__main__":
    main()
