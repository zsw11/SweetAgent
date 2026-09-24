# -*- coding: utf-8 -*-
"""真实链路冒烟（带分阶段计时）：验证 quality_gate 在主图真实运行时正常放行。

依赖真实 LLM + Postgres；用于确认新增质量门不破坏端到端链路。
运行：.venv\\Scripts\\python.exe -u scripts/_smoke_quality_gate.py
"""
import sys
import time

sys.path.insert(0, ".")

from app.observability.logging import setup_logging

setup_logging()
t0 = time.time()


def tick(tag):
    print(f"[smoke] {tag}  +{time.time() - t0:.1f}s", flush=True)


tick("start")
from app.llm import llm_available  # noqa: E402
tick("llm_available checked")
print(f"[smoke] llm_available={llm_available()}", flush=True)

from app.graph.main_graph import run_question  # noqa: E402
tick("imported run_question")

result = run_question(
    "美国市场最近一周销量怎么样？",
    thread_id=f"smoke-qg-{int(time.time() * 1000)}",
    user_id="smoke",
)
tick("run_question done")

qc = result.get("quality_check") or {}
print(f"[smoke] stage={result.get('current_stage')}", flush=True)
print(f"[smoke] quality_check={qc}", flush=True)
print(f"[smoke] final_answer={result.get('final_answer', '')[:80]}", flush=True)
assert not result.get("__interrupt__"), "真实链路不应触发 interrupt（默认非交互）"
assert qc.get("pass") is True, f"quality_gate 应放行，实际 {qc}"
print("[smoke] 真实链路 quality_gate 放行 OK ✅", flush=True)
