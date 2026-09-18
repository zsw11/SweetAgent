-- 1. 看全部 67 张表清单
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
ORDER BY 1;

-- 2. 看最近的订单
SELECT *
FROM orders
ORDER BY order_date DESC
LIMIT 100;

-- 3. 看埋点 SKU（SN-Q12-US）的销量趋势 —— 这就是 Phase 1 Demo 要查的数据
SELECT s.date, ps.sku_code, s.units, s.gmv
FROM sales_daily s
         JOIN product_skus ps ON ps.id = s.sku_id
WHERE ps.sku_code = 'SN-Q12-US'
ORDER BY s.date;

-- 4. 试试 agent_reader 账号执行 INSERT —— 会被拒绝（这是设计好的只读保护）


-- debug
SELECT t.table_name,
       obj_description(c.oid) AS table_comment
FROM information_schema.tables t
         JOIN pg_catalog.pg_class c
              ON c.relname = t.table_name
                  AND c.relnamespace = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = % s)
WHERE t.table_schema = % s
  AND t.table_type = 'BASE TABLE'
  AND (% s::text IS NULL OR t.table_name LIKE %s)
ORDER BY t.table_name
;
SELECT metric_name, definition, formula, unit, department
FROM agent_metric_definition
WHERE (% s::text IS NULL OR metric_name ILIKE %s)
ORDER BY department, metric_name;

SELECT ps.sku_code,
       CASE
           WHEN s.date > (SELECT MAX(date) - 21 FROM mart_sales_daily)
               THEN 'last21'
           ELSE 'prev' END                                      AS segment,
       COUNT(DISTINCT s.date)                                   AS days,
       SUM(s.orders)                                            AS total_orders,
       SUM(s.units)                                             AS total_units,
       ROUND(SUM(s.gmv)::numeric, 2)                            AS total_gmv,
       ROUND(SUM(s.gmv)::numeric / NULLIF(SUM(s.orders), 0), 2) AS avg_order_value,
       ROUND(SUM(s.units)::numeric / COUNT(DISTINCT s.date), 2) AS daily_units
FROM mart_sales_daily s
         JOIN product_skus ps ON ps.id = s.sku_id
         JOIN brands b ON b.id = s.brand_id
WHERE b.name = 'SweetNight'
  AND s.country = 'US'
GROUP BY ps.sku_code, segment
ORDER BY ps.sku_code, segment
LIMIT 100;

WITH base AS (
  SELECT s.date, s.sku_id, s.orders, s.units, s.gmv
  FROM mart_sales_daily s
  JOIN brands b ON b.id = s.brand_id
  WHERE b.name = 'SweetNight'
    AND s.country = 'US'
    AND s.date > (SELECT MAX(date) - INTERVAL '90 days' FROM mart_sales_daily)
),
seg AS (
  SELECT *,
    CASE WHEN date > (SELECT MAX(date) - INTERVAL '21 days' FROM mart_sales_daily) THEN 'last21' ELSE 'prev' END AS period
  FROM base
),
agg AS (
  SELECT
    sku_id,
    period,
    SUM(gmv) AS gmv,
    SUM(orders) AS orders,
    SUM(units) AS units,
    COUNT(DISTINCT date) AS days,
    SUM(gmv) / NULLIF(COUNT(DISTINCT date), 0) AS daily_gmv,
    SUM(orders) / NULLIF(COUNT(DISTINCT date), 0) AS daily_orders,
    SUM(units) / NULLIF(COUNT(DISTINCT date), 0) AS daily_units
  FROM seg
  GROUP BY sku_id, period
),
pivoted AS (
  SELECT
    sku_id,
    MAX(CASE WHEN period = 'prev' THEN daily_gmv END) AS prev_daily_gmv,
    MAX(CASE WHEN period = 'last21' THEN daily_gmv END) AS last21_daily_gmv,
    MAX(CASE WHEN period = 'prev' THEN daily_orders END) AS prev_daily_orders,
    MAX(CASE WHEN period = 'last21' THEN daily_orders END) AS last21_daily_orders,
    MAX(CASE WHEN period = 'prev' THEN daily_units END) AS prev_daily_units,
    MAX(CASE WHEN period = 'last21' THEN daily_units END) AS last21_daily_units
  FROM agg
  GROUP BY sku_id
)
SELECT
  sku_id,
  ROUND(prev_daily_gmv, 2) AS prev_daily_gmv,
  ROUND(last21_daily_gmv, 2) AS last21_daily_gmv,
  ROUND((last21_daily_gmv - prev_daily_gmv) / NULLIF(prev_daily_gmv, 0) * 100, 2) AS gmv_change_pct,
  ROUND(prev_daily_orders, 2) AS prev_daily_orders,
  ROUND(last21_daily_orders, 2) AS last21_daily_orders,
  ROUND((last21_daily_orders - prev_daily_orders) / NULLIF(prev_daily_orders, 0) * 100, 2) AS orders_change_pct,
  ROUND(prev_daily_units, 2) AS prev_daily_units,
  ROUND(last21_daily_units, 2) AS last21_daily_units,
  ROUND((last21_daily_units - prev_daily_units) / NULLIF(prev_daily_units, 0) * 100, 2) AS units_change_pct
FROM pivoted
WHERE prev_daily_gmv IS NOT NULL OR last21_daily_gmv IS NOT NULL
ORDER BY ABS(COALESCE((last21_daily_gmv - prev_daily_gmv) / NULLIF(prev_daily_gmv, 0), 0)) DESC
LIMIT 5000;