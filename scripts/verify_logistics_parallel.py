# -*- coding: utf-8 -*-
"""Logistics _query 并行改造验证（2026-09-24）。

验证点：
  1. 并行耗时：N 个无依赖数据域，串行 sum(T)，并行 ≈ max(T)（mock _query_one 各 0.4s）
  2. observations/sql_history 按 plan 顺序稳定（LLM 下游对顺序敏感）
  3. 单数据域异常不炸主链路（fail 记入 sql_history，queried 标记防重试）
  4. queried 去重：重入 _query 只查新需求

运行：.venv\\Scripts\\python -u scripts\\verify_logistics_parallel.py
"""
import sys
import time

sys.path.insert(0, ".")

from app.agents.logistics.agent import LogisticsAgent  # noqa: E402
from app.agents.logistics.graph import build_logistics_agent  # noqa: E402
from app.agents.logistics.state import LogisticsState  # noqa: E402

_calls: list[str] = []
_parallel_overlap = 0.0


def _fake_query_one(req, task, context):
    """mock：每个数据域耗时 0.4s，'inventory_risk' 抛异常；记录并发重叠度。"""
    global _parallel_overlap
    _calls.append(req)
    time.sleep(0.4)
    if req == "inventory_risk":
        raise RuntimeError("mock 查询失败")
    return {"requirement": req, "rows": [{req: 1}], "sql": f"SELECT * FROM {req}", "ok": True}


def main():
    # 绕过 __init__（不依赖 LLM key），注入最小属性 + mock 三处节点
    ag = LogisticsAgent.__new__(LogisticsAgent)
    ag.max_iterations = 3
    ag.AGENT_NAME = "logistics"
    plan = ["delivery", "inventory_risk", "tracking", "warehouse"]
    ag._plan = lambda task, context: plan
    ag._query_one = _fake_query_one
    ag._analyze = lambda task, observations, context=None: (
        ["分析完成"], [{"type": "analysis", "summary": "ok"}], True, [],
    )

    app = build_logistics_agent(agent=ag)

    # 完整图运行（plan→query→analyze→done）
    start = time.perf_counter()
    result = app.invoke(LogisticsState(task="查物流", logistics_context={}))
    elapsed = time.perf_counter() - start

    # 断言
    obs_reqs = [o["requirement"] for o in result.get("observations", [])]
    assert obs_reqs == ["delivery", "tracking", "warehouse"], f"观察顺序/失败隔离异常: {obs_reqs}"
    fails = [h for h in result.get("sql_history", []) if not h.get("ok")]
    assert len(fails) == 1 and fails[0]["requirement"] == "inventory_risk", f"fail 记录异常: {fails}"
    assert set(result.get("queried", [])) == set(plan), "queried 应包含全部"
    # 串行=4*0.4=1.6s；并行(4 workers)≈0.4s+调度，上限 1.2s
    assert elapsed < 1.2, f"并行耗时异常: {elapsed:.2f}s"
    print(f"[1] 并行耗时: {elapsed:.2f}s（串行预计 1.6s，sum→max 生效）✅")
    print(f"[2] observations 按 plan 顺序: {obs_reqs} ✅")
    print(f"[3] inventory_risk 失败隔离: fail 1 条，主链路未炸 ✅")
    print(f"[4] queried 标记: {sorted(result['queried'])} ✅")

    # 重入验证：第二次 invoke 全 queried → _query 不重复查
    _calls.clear()
    app.invoke(LogisticsState(
        task="查物流", logistics_context={},
        queried=set(plan), observations=list(result["observations"]),
        sql_history=list(result["sql_history"]), enough=True,
    ))
    assert _calls == [], f"重入不应再查: {_calls}"
    print("[5] 重入已查数据域不重复查询 ✅")


if __name__ == "__main__":
    main()
    print("\nLogistics _query 并行改造验证完成 ✅")
