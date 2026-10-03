# -*- coding: utf-8 -*-
"""统计 sweetagent 项目最近 root trace 的真实 LLM 调用数（root 过滤版）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.observability.tracing import get_langsmith_client

client = get_langsmith_client()


def count_llm_under(root_id):
    cnt = 0
    stack = [root_id]
    while stack:
        pid = stack.pop()
        for r in client.list_runs(project_name="sweetagent", parent_run_id=pid):
            if r.run_type == "llm":
                cnt += 1
            stack.append(r.id)
    return cnt


# execution_order=1 表示 root run
roots = list(client.list_runs(project_name="sweetagent", run_type="chain", execution_order=1, limit=15))
print(f"=== 最近 {len(roots)} 条 root trace ===")
for r in roots:
    n = count_llm_under(r.id)
    inp = ""
    if r.inputs:
        s = str(r.inputs)
        inp = s[:70].replace("\n", " ")
    st = r.start_time.strftime("%m-%d %H:%M") if r.start_time else "?"
    print(f"{st} | llm调用={n} | {r.name} | {inp}")
