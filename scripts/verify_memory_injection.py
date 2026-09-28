# -*- coding: utf-8 -*-
"""记忆注入分级验证（考点四十五）：强约束全量 / 弱记忆 top-k / token 预算保强弃弱 / 两入口一致 / exclude_types。

场景：
  1. _fit_budget 纯函数：强优先、超预算截断保强弃弱
  2. build_manager_memory（mock）：rule/preference 低相似度仍全量注入，fact/conclusion top-k，无标签过滤
  3. build_department_memory（mock）：本部门∪无标签的强约束全量 + 弱记忆 top-k + business_rules
  4. 预算超限（count_tokens 放大）：弱记忆先丢、强约束保留
  5. 真实 DB：search_memories exclude_types 生效（uuid 隔离 + 清理）

运行：.venv\\Scripts\\python.exe scripts\\verify_memory_injection.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch  # noqa: E402

from app.memory import injection as inj  # noqa: E402
from app.memory.injection import (  # noqa: E402
    _fit_budget,
    build_department_memory,
    build_manager_memory,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


# ---------------------------------------------------------------------------
# mock 记忆库（8 条：低相似度强约束、无标签/带标签混合）
# ---------------------------------------------------------------------------
_FAKE_MEMORIES = [
    {"id": 1, "memory_type": "rule", "content": "用户要求 GMV 统计口径含退款前金额", "department": None,
     "confidence": 0.95, "evidence": "原话", "similarity": 0.21},
    {"id": 2, "memory_type": "preference", "content": "报告默认用美元结算", "department": None,
     "confidence": 0.90, "evidence": "原话", "similarity": 0.30},
    {"id": 3, "memory_type": "fact", "content": "美国床垫 9 月销量下滑 26%", "department": None,
     "confidence": 0.80, "evidence": "原话", "similarity": 0.48},
    {"id": 4, "memory_type": "fact", "content": "SN-Q12-US 是畅销 SKU", "department": None,
     "confidence": 0.70, "evidence": "原话", "similarity": 0.45},
    {"id": 5, "memory_type": "fact", "content": "退货率环比上升 2%", "department": None,
     "confidence": 0.75, "evidence": "原话", "similarity": 0.42},
    {"id": 6, "memory_type": "conclusion", "content": "上次分析认为时效是退货主因", "department": None,
     "confidence": 0.60, "evidence": "原话", "similarity": 0.40},
    {"id": 7, "memory_type": "conclusion", "content": "广告 ROI 持续下降是获客问题", "department": None,
     "confidence": 0.60, "evidence": "原话", "similarity": 0.35},
    {"id": 8, "memory_type": "rule", "content": "operation 部门规则：日报必须含环比", "department": "operation",
     "confidence": 0.90, "evidence": "原话", "similarity": 0.28},
]


def _fake_search(user_id: str, query_text: str, departments=None, top_k: int = 5,
                 memory_type=None, exclude_types=None) -> list[dict]:
    """模拟 search_memories：类型过滤 + 部门粗筛（department ∈ 列表 ∪ 无标签）+ 相似度降序。"""
    items = [dict(m) for m in _FAKE_MEMORIES]
    if memory_type:
        items = [m for m in items if m["memory_type"] == memory_type]
    if exclude_types:
        items = [m for m in items if m["memory_type"] not in exclude_types]
    if departments is not None:
        items = [m for m in items if m["department"] in departments or m["department"] is None]
    items.sort(key=lambda m: m["similarity"], reverse=True)
    return items[:top_k]


# ---------------------------------------------------------------------------
# 场景 1：_fit_budget
# ---------------------------------------------------------------------------
def test_fit_budget() -> None:
    print("[1] _fit_budget 预算截断（纯函数）")
    strong = [{"id": 1, "memory_type": "rule", "content": "规则文本", "similarity": 0.2}]
    weak = [
        {"id": 2, "memory_type": "fact", "content": "事实A", "similarity": 0.9},
        {"id": 3, "memory_type": "fact", "content": "事实B", "similarity": 0.8},
    ]
    with patch.object(inj, "count_tokens", lambda s: 100):
        kept, dropped, used = _fit_budget(strong, weak, 250)
    check("强约束保留", any(m["id"] == 1 for m in kept), str(kept))
    check("弱记忆按相似度只进 1 条", sum(1 for m in kept if m["id"] in (2, 3)) == 1, str(kept))
    check("弱记忆丢弃 1 条", dropped == 1, str(dropped))
    check("占用 token ≤ 预算", used <= 250, str(used))


# ---------------------------------------------------------------------------
# 场景 2：Manager 注入分级
# ---------------------------------------------------------------------------
def test_manager() -> None:
    print("[2] build_manager_memory 分级注入（mock）")
    with patch.object(inj, "get_profiles", lambda uid: {"role": "市场负责人"}), \
         patch.object(inj, "get_preferences", lambda uid: {}), \
         patch.object(inj, "search_memories", _fake_search):
        mem = build_manager_memory("u", "美国床垫销量分析")
    mems = mem.get("memories", [])
    check("强约束低相似度仍注入（rule）", any("GMV 统计口径" in m for m in mems), str(mems))
    check("偏好注入（preference）", any("美元结算" in m for m in mems), str(mems))
    check("带部门标签的强约束被 Manager 过滤", not any("日报必须含环比" in m for m in mems), str(mems))
    weak_count = sum(1 for m in mems if m.startswith("（事实）") or m.startswith("（历史结论）"))
    check("弱记忆 top-k=5（相似度最高的弱类型）", weak_count == 5, str(weak_count))
    check("类型标注保留", any(m.startswith("（规则）") for m in mems), str(mems))


# ---------------------------------------------------------------------------
# 场景 3：部门注入分级
# ---------------------------------------------------------------------------
def test_department() -> None:
    print("[3] build_department_memory 分级注入（mock）")
    with patch.object(inj, "get_business_preferences", lambda scope: {"report_rule": "日报含环比"}), \
         patch.object(inj, "search_memories", _fake_search):
        ctx = build_department_memory("u", "operation", "美国床垫销量分析")
    check("business_rules 注入", ctx.get("business_rules", {}).get("report_rule") == "日报含环比", str(ctx))
    mems = ctx.get("department_memories", [])
    check("部门含本部门规则", any("日报必须含环比" in m for m in mems), str(mems))
    check("部门含无标签强约束", any("GMV 统计口径" in m for m in mems) and any("美元结算" in m for m in mems), str(mems))
    weak_count = sum(1 for m in mems if m.startswith("（事实）") or m.startswith("（历史结论）"))
    check("弱记忆 top-k ≤ 5", 0 < weak_count <= 5, str(weak_count))


# ---------------------------------------------------------------------------
# 场景 4：预算超限 → 保强弃弱
# ---------------------------------------------------------------------------
def test_budget_overflow() -> None:
    print("[4] 预算超限：保强弃弱")
    with patch.object(inj, "get_profiles", lambda uid: {}), \
         patch.object(inj, "get_preferences", lambda uid: {}), \
         patch.object(inj, "search_memories", _fake_search), \
         patch.object(inj, "count_tokens", lambda s: 400):
        # budget=1500：强 2 条=800，弱第 1 条(销量下滑,sim0.48)=1200 进；第 2 条(SN-Q12)=1600>1500 丢
        mem = build_manager_memory("u", "美国床垫销量分析")
    mems = mem.get("memories", [])
    check("强约束 2 条全保留", any("GMV 统计口径" in m for m in mems) and any("美元结算" in m for m in mems), str(mems))
    check("弱记忆只进相似度最高的 1 条", any("9 月销量下滑" in m for m in mems), str(mems))
    check("其余弱记忆被丢弃", not any("SN-Q12" in m for m in mems) and not any("退货率环比" in m for m in mems), str(mems))
    check("总注入条数=3", len(mems) == 3, str(len(mems)))


# ---------------------------------------------------------------------------
# 场景 5：真实 DB exclude_types
# ---------------------------------------------------------------------------
def test_db_exclude() -> None:
    print("[5] 真实 DB：search_memories exclude_types")
    from app.memory.db import connect, resolve_user_id
    from app.memory.embeddings import mock_embedding, vector_to_sql
    from app.memory.semantic import search_memories

    username = f"mem_inj_{uuid.uuid4().hex[:8]}"
    uid = resolve_user_id(username)
    try:
        with connect() as conn:
            for t, content in [
                ("rule", "测试规则：所有报表必须含环比"),
                ("fact", "测试事实：美国床垫销量下滑"),
                ("conclusion", "测试结论：时效是退货主因"),
            ]:
                conn.execute(
                    "INSERT INTO user_memories (user_id, memory_type, content, metadata, embedding) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (uid, t, content, None, vector_to_sql(mock_embedding(content))),
                )
            conn.commit()

        all_types = search_memories(username, "报表", top_k=10)
        check("全量检索返回 3 条", len(all_types) == 3, str(len(all_types)))
        weak_only = search_memories(username, "报表", top_k=10, exclude_types=["preference", "rule"])
        check("exclude 强类型后仅弱记忆", all(m["memory_type"] in ("fact", "conclusion") for m in weak_only), str(weak_only))
        check("exclude 后条数=2", len(weak_only) == 2, str(len(weak_only)))
        rule_only = search_memories(username, "报表", top_k=10, memory_type="rule")
        check("memory_type 过滤仅 rule", len(rule_only) == 1 and rule_only[0]["memory_type"] == "rule", str(rule_only))
    finally:
        with connect() as conn:
            conn.execute("DELETE FROM user_memories WHERE user_id = %s", (uid,))
            conn.execute("DELETE FROM users WHERE id = %s", (uid,))
            conn.commit()


# ---------------------------------------------------------------------------
# 场景 6：弱记忆 TTL 软过期 / 强约束常驻（块B）
# ---------------------------------------------------------------------------
def test_ttl_expire() -> None:
    print("[6] 真实 DB：弱记忆 TTL 软过期 / 强约束常驻")
    from app.memory.db import connect, resolve_user_id
    from app.memory.embeddings import mock_embedding, vector_to_sql
    from app.memory.semantic import expire_stale_memories, search_memories

    username = f"mem_ttl_{uuid.uuid4().hex[:8]}"
    uid = resolve_user_id(username)
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO user_memories (user_id, memory_type, content, metadata, embedding, updated_at) "
                "VALUES (%s, 'fact', %s, %s, %s, now() - interval '100 days')",
                (uid, "过期事实：美国床垫老结论", None, vector_to_sql(mock_embedding("过期事实"))),
            )
            conn.execute(
                "INSERT INTO user_memories (user_id, memory_type, content, metadata, embedding, updated_at) "
                "VALUES (%s, 'rule', %s, %s, %s, now() - interval '100 days')",
                (uid, "常驻规则：报表用美元", None, vector_to_sql(mock_embedding("常驻规则"))),
            )
            conn.execute(
                "INSERT INTO user_memories (user_id, memory_type, content, metadata, embedding, updated_at) "
                "VALUES (%s, 'fact', %s, %s, %s, now())",
                (uid, "新事实：美国床垫本月销量回升", None, vector_to_sql(mock_embedding("新事实"))),
            )
            conn.commit()

        n = expire_stale_memories(username)
        check("只过期 1 条弱记忆", n == 1, str(n))
        with connect() as conn:
            rows = conn.execute(
                "SELECT memory_type, content, superseded_at FROM user_memories "
                "WHERE user_id = %s ORDER BY id", (uid,),
            ).fetchall()
        old_fact = [r for r in rows if r[1] == "过期事实：美国床垫老结论"][0]
        check("过期弱记忆软删除（superseded_at 非空）", old_fact[2] is not None, str(old_fact))
        old_rule = [r for r in rows if r[1] == "常驻规则：报表用美元"][0]
        check("强约束常驻（superseded_at 为空）", old_rule[2] is None, str(old_rule))
        new_fact = [r for r in rows if r[1] == "新事实：美国床垫本月销量回升"][0]
        check("新弱记忆不过期", new_fact[2] is None, str(new_fact))

        res = search_memories(username, "报表", top_k=10)
        check("检索不含已过期弱记忆", not any("老结论" in m["content"] for m in res), str(res))
        check("检索仍含常驻规则", any("报表用美元" in m["content"] for m in res), str(res))
        with connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM user_memories WHERE user_id = %s", (uid,),
            ).fetchone()[0]
        check("物理行保留（软删除可追溯）", total == 3, str(total))
    finally:
        with connect() as conn:
            conn.execute("DELETE FROM user_memories WHERE user_id = %s", (uid,))
            conn.execute("DELETE FROM users WHERE id = %s", (uid,))
            conn.commit()


# ---------------------------------------------------------------------------
# 场景 7：用户显式纠错（块C·M档：target 定位 + 以用户为准）
# ---------------------------------------------------------------------------
def test_user_correction() -> None:
    print("[7] 真实 DB：用户显式纠错（M档：target 定位 + 以用户为准）")
    from app.memory.db import connect, resolve_user_id
    from app.memory.embeddings import mock_embedding, vector_to_sql
    from app.memory.semantic import add_memory, search_memories

    # case A：target 精确命中 → 版本化替换（content 反转仍生效：下滑→回升）
    ua = f"mem_corr_a_{uuid.uuid4().hex[:8]}"
    uid_a = resolve_user_id(ua)
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO user_memories (user_id, memory_type, content, metadata, embedding) "
                "VALUES (%s, 'fact', %s, %s, %s)",
                (uid_a, "美国床垫9月销量下滑26%", None,
                 vector_to_sql(mock_embedding("美国床垫9月销量下滑26%"))),
            )
            conn.commit()
        mid = add_memory(ua, "fact", "美国床垫9月销量回升5%",
                         user_correction=True, correction_target="美国床垫9月销量下滑26%")
        with connect() as conn:
            rows = conn.execute(
                "SELECT content, superseded_at, metadata FROM user_memories "
                "WHERE user_id = %s ORDER BY id", (uid_a,),
            ).fetchall()
        old = [r for r in rows if "下滑26%" in r[0]][0]
        new = [r for r in rows if "回升5%" in r[0]][0]
        check("A: 返回新记忆 id>0", mid > 0, str(mid))
        check("A: 旧条被软删除（superseded_at 非空）", old[1] is not None, str(old))
        check("A: 新条为纠正内容且未取代", new[1] is None and "回升5%" in new[0], str(new))
        check("A: metadata 记 user_correction + target",
              (new[2] or {}).get("source") == "user_correction" and bool((new[2] or {}).get("correction_target")),
              str(new[2]))
        res = search_memories(ua, "销量", top_k=10)
        check("A: 检索返回纠正内容、不含旧条",
              any("回升5%" in m["content"] for m in res) and not any("下滑26%" in m["content"] for m in res),
              str(res))
    finally:
        with connect() as conn:
            conn.execute("DELETE FROM user_memories WHERE user_id = %s", (uid_a,))
            conn.execute("DELETE FROM users WHERE id = %s", (uid_a,))
            conn.commit()

    # case B：target 未命中 → content 回退也未命中 → 新增（无关旧条不误伤）
    ub = f"mem_corr_b_{uuid.uuid4().hex[:8]}"
    uid_b = resolve_user_id(ub)
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO user_memories (user_id, memory_type, content, metadata, embedding) "
                "VALUES (%s, 'fact', %s, %s, %s)",
                (uid_b, "广告投放ROAS红线是3", None,
                 vector_to_sql(mock_embedding("广告投放ROAS红线是3"))),
            )
            conn.commit()
        mid = add_memory(ub, "fact", "美国床垫销量回升5%",
                         user_correction=True, correction_target="完全不存在的旧内容XYZ")
        with connect() as conn:
            rows = conn.execute(
                "SELECT content, superseded_at, metadata FROM user_memories "
                "WHERE user_id = %s ORDER BY id", (uid_b,),
            ).fetchall()
        newb = [r for r in rows if "回升5%" in r[0]][0]
        oldb = [r for r in rows if "ROAS红线" in r[0]][0]
        check("B: target 未命中时新增（source=user_correction）",
              mid > 0 and (newb[2] or {}).get("source") == "user_correction", str(newb))
        check("B: 无关旧条未被误伤（未 superseded）", oldb[1] is None, str(oldb))
    finally:
        with connect() as conn:
            conn.execute("DELETE FROM user_memories WHERE user_id = %s", (uid_b,))
            conn.execute("DELETE FROM users WHERE id = %s", (uid_b,))
            conn.commit()

    # case C：user_correction=False 普通路径不变（exact duplicate → refresh，不新增不取代）
    uc = f"mem_corr_c_{uuid.uuid4().hex[:8]}"
    uid_c = resolve_user_id(uc)
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO user_memories (user_id, memory_type, content, metadata, embedding) "
                "VALUES (%s, 'fact', %s, %s, %s)",
                (uid_c, "美国床垫9月销量回升5%", None,
                 vector_to_sql(mock_embedding("美国床垫9月销量回升5%"))),
            )
            conn.commit()
        mid = add_memory(uc, "fact", "美国床垫9月销量回升5%")  # 普通路径
        with connect() as conn:
            rows = conn.execute(
                "SELECT content, superseded_at FROM user_memories WHERE user_id = %s", (uid_c,),
            ).fetchall()
        check("C: 普通模式 exact duplicate → 刷新旧条不新增",
              mid > 0 and len(rows) == 1 and rows[0][1] is None, str(rows))
    finally:
        with connect() as conn:
            conn.execute("DELETE FROM user_memories WHERE user_id = %s", (uid_c,))
            conn.execute("DELETE FROM users WHERE id = %s", (uid_c,))
            conn.commit()


def main() -> None:
    test_fit_budget()
    test_manager()
    test_department()
    test_budget_overflow()
    test_db_exclude()
    test_ttl_expire()
    test_user_correction()
    print(f"\n结果: PASS {PASS} / FAIL {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
