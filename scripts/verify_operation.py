"""Operation Agent 验证/调试脚本（Phase 1 最小闭环）。

用途：
- 验证 Operation Agent 双入口：主循环 agent.run()（命令式）与 SubGraph run_operation()（LangGraph）
- 问题不写死，三种方式传入：命令行位置参数 > 环境变量 VERIFY_TASK > 交互输入
- 调试开关见文件底部常量：RUN_SUBGRAPH（IDE 打断点无需传参；当前仅 LLM 模式）

用法：
    .venv\\Scripts\\python scripts\\verify_operation.py "你的问题"     # 方式1：命令行参数
    set VERIFY_TASK=你的问题 && .venv\\Scripts\\python scripts\\verify_operation.py  # 方式2：环境变量
    .venv\\Scripts\\python scripts\\verify_operation.py                 # 方式3：交互输入
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, ".")  # 确保从项目根导入

from app.agents.operation.agent import OperationAgent
from app.agents.operation.graph import run_operation
from app.llm import llm_available

# ---------------- 调试开关（IDE Debug 打断点无需传参） ----------------
RUN_SUBGRAPH = True     # True=同时跑 SubGraph 入口对比；False=只跑主循环
# ----------------------------------------------------------------------


def _resolve_task() -> str:
    """解析要分析的问题：命令行位置参数 > 环境变量 VERIFY_TASK > 交互输入。"""
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        return " ".join(sys.argv[1:])
    env = os.environ.get("VERIFY_TASK")
    if env:
        return env
    return input("请输入要分析的问题（回车使用默认示例）: ").strip() or (
        "分析美国市场过去90天 SweetNight 床垫 GMV、订单、销量变化，并找出异常 SKU"
    )


def print_result(result: dict, title: str) -> None:
    """打印一次运行的 Operation Result。"""
    print(f"\n{title}")
    print(f"  summary: {result.get('summary')}")
    metrics = result.get("metrics") or []
    print(f"  metrics: {len(metrics)} 个")
    for m in metrics:
        print(f"    - {m.get('name')}: prev={m.get('prev')} -> last21={m.get('last21')} ({m.get('change_pct')}%)")
    print(f"  anomalies: {json.dumps(result.get('anomalies', []), ensure_ascii=False)}")
    print(f"  confidence: {result.get('confidence')} | mode: {result.get('mode')}")
    print(f"  sql_history: {len(result.get('sql_history') or [])} 条")


def main() -> None:
    task = _resolve_task()
    print(f"问题: {task}")
    if not llm_available():
        print("❌ 未配置 LLM API Key（如 DEEPSEEK_API_KEY）。当前仅支持 LLM 模式，请在 .env 配置后重试。")
        sys.exit(1)
    print("LLM 可用: True（模式: llm）")

    # A. 主循环入口：命令式内部循环
    agent = OperationAgent()
    result = agent.run(task)
    print_result(result, "== [主循环] agent.run() ==")

    # B. SubGraph 入口：LangGraph StateGraph（可选，对比两种调用形态）
    if RUN_SUBGRAPH:
        sub = run_operation(task)
        print_result(sub, "== [SubGraph] run_operation() ==")

    print("\n✅ 调试完成")


if __name__ == "__main__":
    main()
