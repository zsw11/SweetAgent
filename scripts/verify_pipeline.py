"""全链路验证脚本（Phase 1 最小闭环：Manager -> Operation -> Decision）。

用途：
- 验证主 Graph 端到端执行：Manager 规划 -> Router 调度 -> Operation 分析 -> Decision 汇总
- 问题不写死：命令行位置参数 > 环境变量 VERIFY_TASK > 交互输入
- 输出完整执行轨迹与结构化决策报告

用法：
    .venv\\Scripts\\python scripts\\verify_pipeline.py "你的问题"
    set VERIFY_TASK=你的问题 && .venv\\Scripts\\python scripts\\verify_pipeline.py
    .venv\\Scripts\\python scripts\\verify_pipeline.py
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, ".")

from app.graph.main_graph import run_question
from app.llm import llm_available


def _resolve_task() -> str:
    """解析问题：命令行 > 环境变量 > 交互输入。"""
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        return " ".join(sys.argv[1:])
    env = os.environ.get("VERIFY_TASK")
    if env:
        return env
    return input("请输入要分析的问题（回车使用默认示例）: ").strip() or (
        "分析 SweetNight 品牌美国市场过去90天各SKU的GMV、订单、销量变化，并找出异常SKU"
    )


def print_task_plan(plan: dict) -> None:
    """打印 Manager 规划结果。"""
    print("\n" + "=" * 60)
    print("== [Manager] 任务规划 ==")
    print("=" * 60)
    print(f"  intent: {plan.get('intent')}")
    print(f"  required_agents: {plan.get('required_agents')}")
    print(f"  tasks ({len(plan.get('tasks', []))}):")
    for t in plan.get("tasks", []):
        deps = t.get("depends_on", [])
        dep_str = f" -> depends_on={deps}" if deps else ""
        print(f"    - [{t.get('agent'):10s}] {t.get('id')}{dep_str}")
        if t.get("description"):
            print(f"        {t.get('description')}")


def print_department_results(results: dict) -> None:
    """打印各部门结果摘要。"""
    print("\n" + "=" * 60)
    print("== [部门 Agent] 分析结果 ==")
    print("=" * 60)
    for name, result in results.items():
        print(f"\n  --- {name} ---")
        print(f"  summary: {result.get('summary', '')[:200]}")
        metrics = result.get("metrics") or []
        if metrics:
            print(f"  metrics ({len(metrics)}):")
            for m in metrics[:5]:
                print(f"    - {m.get('name')}: prev={m.get('prev')} -> last21={m.get('last21')} (change={m.get('change_pct')}%)")
        anomalies = result.get("anomalies") or []
        if anomalies:
            print(f"  anomalies ({len(anomalies)}):")
            for a in anomalies[:5]:
                print(f"    - {a.get('sku')}: {a.get('indicator')} change={a.get('change_pct')}%")
        print(f"  confidence: {result.get('confidence')}")
        if result.get("error"):
            print(f"  ⚠️ error: {result.get('error')}")


def print_decision(report: dict) -> None:
    """打印 Decision Agent 结构化报告。"""
    print("\n" + "=" * 60)
    print("== [Decision] 最终决策报告 ==")
    print("=" * 60)
    print(f"\n  📌 核心结论: {report.get('summary', '')}")

    findings = report.get("findings") or []
    if findings:
        print(f"\n  🔍 关键发现 ({len(findings)}):")
        for f in findings:
            cat = f.get("category", "?")
            text = f.get("finding", "")
            print(f"    [{cat:10s}] {text}")

    root_causes = report.get("root_causes") or []
    if root_causes:
        print(f"\n  🎯 根因分析 ({len(root_causes)}):")
        for rc in root_causes:
            print(f"    - 原因: {rc.get('cause', '')}")
            print(f"      证据: {rc.get('evidence', '')}")

    recommendations = report.get("recommendations") or []
    if recommendations:
        print(f"\n  💡 行动建议 ({len(recommendations)}):")
        for r in recommendations:
            priority = r.get("priority", "?")
            action = r.get("action", "")
            print(f"    [{priority}] {action}")

    risks = report.get("risks") or []
    if risks:
        print(f"\n  ⚠️ 风险提示 ({len(risks)}):")
        for r in risks:
            print(f"    - [{r.get('severity', '?')}] {r.get('risk', '')}")

    print(f"\n  📊 置信度: {report.get('confidence', 0):.2f}")


def main() -> None:
    question = _resolve_task()
    print(f"问题: {question}")

    if not llm_available():
        print("❌ 未配置 LLM API Key（如 DEEPSEEK_API_KEY）。请在 .env 配置后重试。")
        sys.exit(1)
    print("LLM 可用: True")

    t0 = time.time()
    print("\n🚀 开始执行主 Graph（Manager -> Operation -> Decision）...")

    result = run_question(question, thread_id="verify-pipeline", user_id="dev")

    elapsed = time.time() - t0
    print(f"\n⏱️  总耗时: {elapsed:.1f}s")
    print(f"📋 执行阶段: {result.get('current_stage')}")
    print(f"✅ 已完成任务: {result.get('completed_tasks')}")
    skipped = result.get("skipped_tasks") or []
    if skipped:
        print(f"⏭️  跳过任务（Agent 未实现）: {skipped}")

    # 打印各阶段结果
    print_task_plan(result.get("task_plan") or {})
    print_department_results(result.get("department_results") or {})
    print_decision(result.get("decision_result") or {})

    # 错误检查
    if result.get("current_stage") == "error":
        print(f"\n❌ 执行出错: {result.get('error_state')}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("✅ 全链路验证完成（Manager -> Operation -> Decision）")
    print("=" * 60)


if __name__ == "__main__":
    main()
