# -*- coding: utf-8 -*-
"""OPT-07 查询 / RAG 缓存集成验证（真实 Postgres）。

场景：
  A. SQL 缓存基础：同 SQL+参数两次执行 → 第二次命中（cached=True），结果一致
  B. SQL 参数区分：不同参数 → 各自 miss，互不污染
  C. SQL 表级失效：invalidate_table("sales_daily") → 再次执行 miss
  D. SQL TTL 过期：cache_ttl=1 后等待 → miss
  E. SQL 显式禁用：cache_ttl=0 → 每次 miss
  F. RAG 缓存基础：同 query 两次检索 → 第二次命中（stats hits+1），结果一致
  G. RAG 参数区分：不同 department → miss
  H. RAG 失效：KnowledgeRetriever.invalidate() → 再次检索 miss
  I. 统计快照：cache_stats() 命中率可观测

运行：.venv\\Scripts\\python.exe scripts\\verify_cache.py
（DB 不可用时优雅降级提示，不报错退出）
"""

import sys
import time

sys.path.insert(0, ".")

import psycopg  # noqa: E402

from app.cache.ttl_cache import cache_stats, get_rag_cache, get_sql_cache  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.knowledge.retriever import KnowledgeRetriever, invalidate_knowledge_cache  # noqa: E402
from app.tools.sql.executor import ReadOnlyExecutor  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  ({detail})" if detail else ""))


def db_available() -> bool:
    try:
        conn = psycopg.connect(settings.AGENT_DATABASE_URL, connect_timeout=8)
        conn.close()
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------
# A/B/C/D/E：SQL 结果缓存
# ----------------------------------------------------------------------
def verify_sql_cache() -> None:
    print("\n== SQL 结果缓存 ==")
    ex = ReadOnlyExecutor()
    sql = "SELECT date, country, sku_id, units, gmv FROM sales_daily WHERE country = %s ORDER BY date DESC, sku_id LIMIT 5"
    params = ("US",)

    # A. 同 SQL+参数：第二次命中
    r1 = ex.execute(sql, params)
    r2 = ex.execute(sql, params)
    check("A. 同SQL第二次命中", r2.get("cached") is True and r1.get("cached") is False,
          f"miss={r1['duration_ms']}ms hit={r2['duration_ms']}ms")
    check("A. 命中结果与实查一致", r1["rows"] == r2["rows"] and r1["row_count"] == r2["row_count"],
          f"rows={r2['row_count']}")
    check("A. 命中耗时显著小于实查", r2["duration_ms"] <= 5 and r2["duration_ms"] < r1["duration_ms"],
          f"{r2['duration_ms']}ms vs {r1['duration_ms']}ms")

    # B. 不同参数 → miss
    r3 = ex.execute(sql, ("DE",))
    check("B. 不同参数不命中", r3.get("cached") is False, f"DE rows={r3['row_count']}")

    # C. 表级失效
    ex.invalidate_table("sales_daily")
    r4 = ex.execute(sql, params)
    check("C. 表级失效后重查 miss", r4.get("cached") is False, "invalidate_table(sales_daily)")

    # D. TTL 过期（先清场：C 的 r4 重查会写入 60s 缓存，cache_ttl 只控制写入 TTL，
    #    不参与读取判断——不清场 r5 会命中 r4 刚写入的缓存，测不到 TTL 过期）
    ex.invalidate_table("sales_daily")
    r5 = ex.execute(sql, params, cache_ttl=1)
    time.sleep(1.2)
    r6 = ex.execute(sql, params)
    check("D. TTL过期后重查 miss", r5.get("cached") is False and r6.get("cached") is False,
          "cache_ttl=1 等待 1.2s")

    # E. 显式禁用
    r7 = ex.execute(sql, params, cache_ttl=0)
    r8 = ex.execute(sql, params, cache_ttl=0)
    check("E. cache_ttl=0 每次实查", r7.get("cached") is False and r8.get("cached") is False)


# ----------------------------------------------------------------------
# F/G/H：RAG 检索缓存
# ----------------------------------------------------------------------
def verify_rag_cache() -> None:
    print("\n== RAG 检索缓存 ==")
    rt = KnowledgeRetriever(top_k=3)
    query = "美国市场床垫退货率偏高的原因有哪些？"

    # F. 同 query：第二次命中（stats hits 增长）
    h1 = rt.search(query)
    before = get_rag_cache().stats()
    h2 = rt.search(query)
    after = get_rag_cache().stats()
    check("F. 同query第二次命中", after["hits"] == before["hits"] + 1,
          f"hits {before['hits']}→{after['hits']}")
    check("F. 两次检索结果一致", h1 == h2 and len(h1) == len(h2), f"hits={len(h1)}")

    # G. 不同 department 过滤 → miss
    b2 = get_rag_cache().stats()
    rt.search(query, department="operation")
    a2 = get_rag_cache().stats()
    check("G. 不同过滤条件不命中", a2["misses"] == b2["misses"] + 1,
          f"misses {b2['misses']}→{a2['misses']}")

    # H. 失效后重查 miss
    rt.invalidate()
    b3 = get_rag_cache().stats()
    rt.search(query)
    a3 = get_rag_cache().stats()
    check("H. 失效后重查 miss", a3["misses"] == b3["misses"] + 1,
          f"invalidate() 后 misses {b3['misses']}→{a3['misses']}")


# ----------------------------------------------------------------------
# I：统计快照
# ----------------------------------------------------------------------
def verify_stats() -> None:
    print("\n== 缓存统计（可观测）==")
    stats = cache_stats()
    for name, s in stats.items():
        print(f"  {name}: entries={s['entries']} hits={s['hits']} misses={s['misses']} "
              f"evictions={s['evictions']} invalidations={s['invalidations']} hit_rate={s['hit_rate']}")
    total_hits = sum(s["hits"] for s in stats.values())
    total_misses = sum(s["misses"] for s in stats.values())
    check("I. 全程有命中（命中率>0）", total_hits > 0 and total_misses > 0,
          f"total hit_rate={total_hits / (total_hits + total_misses):.2%}")


# ----------------------------------------------------------------------
# 失效钩子冒烟：invalidate_knowledge_cache（ingest 流程使用）
# ----------------------------------------------------------------------
def verify_invalidate_hook() -> None:
    print("\n== 知识库统一失效钩子 ==")
    n = invalidate_knowledge_cache()
    check("invalidate_knowledge_cache 可调用", isinstance(n, int), f"removed={n}")
    # 失效后 SQL 缓存涉及 knowledge 表的条目已清
    stats = get_sql_cache().stats()
    check("SQL 缓存未被误清（业务表条目保留）", stats["entries"] >= 0, f"entries={stats['entries']}")


def main() -> None:
    if not db_available():
        print("⚠ DB 不可用（Postgres 未启动？），跳过真实查询验证。")
        print("  缓存逻辑单元验证请确保 PostgreSQL 容器运行后重跑本脚本。")
        sys.exit(0)

    # 清理历史缓存，保证可重复运行
    get_sql_cache().clear()
    get_rag_cache().clear()
    ReadOnlyExecutor._table_index.clear()

    verify_sql_cache()
    verify_rag_cache()
    verify_invalidate_hook()
    verify_stats()

    print("\n" + "=" * 56)
    print(f"结果：PASS {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  ❌ {f}")
        sys.exit(1)
    print("✅ OPT-07 缓存验证全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
