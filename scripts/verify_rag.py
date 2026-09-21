# -*- coding: utf-8 -*-
"""RAG 检索验证脚本：验证知识库向量检索的命中、部门过滤与关键词兜底。

用例（与种子知识文档对齐）：
1. 跨部门检索：问产品知识/运营规则/财务口径/物流规则，应命中对应文档；
2. 部门过滤：指定 department 后不应返回其他部门文档；
3. 关键词兜底：无强向量信号的 query 触发 ILIKE 兜底（method=keyword）。

用法：
    .venv\\Scripts\\python scripts\\verify_rag.py
    .venv\\Scripts\\python scripts\\verify_rag.py --verbose   # 打印命中内容
"""
import sys
sys.path.insert(0, ".")

from app.knowledge.retriever import KnowledgeRetriever

CASES = [
    # (说明, query, department, 期望命中文档标题关键词)
    ("产品-市场趋势", "美国床垫市场趋势和主流尺寸是什么", "product", "市场趋势"),
    ("产品-开发流程", "公司新品开发流程是什么", "product", "新品开发SOP"),
    ("运营-广告红线", "广告投放的ROAS红线是多少", "operation", "广告投放运营SOP"),
    ("财务-毛利口径", "贡献毛利怎么计算、口径是什么", "finance", "核算口径"),
    ("财务-退款预警", "退款率超过多少需要预警", "finance", "退款与对账"),
    ("物流-库存阈值", "库存天数低于多少触发补货预警", "logistics", "库存预警"),
    ("物流-履约时效", "美国市场订单的履约SLA要求", "logistics", "物流SLA"),
    ("全局-经营红线", "公司的经营红线有哪些", "company", "经营规则"),
]


def run(verbose: bool = False) -> None:
    retriever = KnowledgeRetriever(top_k=3)
    passed, failed = 0, []
    for label, query, dept, expect in CASES:
        hits = retriever.search(query, department=dept)
        top = hits[0] if hits else {}
        ok = bool(top) and expect in (top.get("title") or "")
        print(f"[{'PASS' if ok else 'FAIL'}] {label}: 问「{query}」→ "
              f"{top.get('title', '无命中')} (sim={top.get('similarity')}, method={top.get('method')})")
        if verbose and hits:
            for h in hits[:2]:
                print(f"    · {h['content'][:60]}...")
        if ok:
            passed += 1
        else:
            failed.append(label)

    # 部门过滤：财务问广告规则不应命中运营文档
    print("\n[部门过滤测试] 财务部门检索「广告ROAS」:")
    hits = retriever.search("广告ROAS红线", department="finance")
    for h in hits:
        print(f"    · {h['title']} (dept={h['department']}, sim={h['similarity']})")
    leak = any(h["department"] != "finance" for h in hits)
    print(f"[{'FAIL' if leak else 'PASS'}] 部门过滤{'存在泄漏!' if leak else '正常'}")

    # 关键词兜底：模拟向量无法区分的冷门词触发 ILIKE
    print("\n[关键词兜底测试] query=「儿童安全认证」(低频词):")
    hits = retriever.search("儿童安全认证", department="operation", min_score=0.99)  # 强制触发兜底
    for h in hits[:3]:
        print(f"    · {h['title']} (method={h.get('method')}, sim={h.get('similarity')})")

    print(f"\n结果: {passed}/{len(CASES)} 用例通过" + (f"，失败: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    run(verbose="--verbose" in sys.argv)
