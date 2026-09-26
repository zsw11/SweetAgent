# -*- coding: utf-8 -*-
"""LangSmith 接入验证（OPT-02）。

三问验证：① API Key 有效；② 网络可达；③ 项目可见性。
- 模式 1（默认）：只验证 Client 初始化 + 项目可见（零 LLM 成本）；
- 模式 2（--run 1）：额外跑一次真实提问（run_question），验证全链路 trace 上云，
  并打印最近 trace 的 URL 供面板核对。

用法（项目根目录）：
    .venv\\Scripts\\python scripts\\verify_langsmith.py
    .venv\\Scripts\\python scripts\\verify_langsmith.py --run 1 --question "6 月各平台广告花费是多少？"
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

from app.config.settings import settings  # noqa: E402
from app.observability.logging import setup_logging  # noqa: E402
from app.observability.tracing import (  # noqa: E402
    init_langsmith,
    list_recent_runs,
)

setup_logging()


def verify_client() -> None:
    client = init_langsmith()
    print("=" * 60)
    print("① API Key / 网络 / 项目可见性验证")
    print("=" * 60)
    if client is None:
        print("❌ Client 初始化失败（未启用 / key 缺失 / 网络失败），见日志 langsmith.*")
        print(f"   当前配置：LANGSMITH_TRACING={settings.LANGSMITH_TRACING} "
              f"API_KEY={'***' if settings.LANGSMITH_API_KEY else '(空)'} "
              f"PROJECT={settings.LANGSMITH_PROJECT}")
        return
    print(f"✅ Client 初始化成功")
    print(f"   API 端点 : {getattr(client, 'api_url', '')}")
    try:
        projs = client.list_projects(limit=10)
        names = [p.name for p in projs]
        print(f"✅ 网络可达，可见项目 ({len(names)} 个)：{names}")
        ok = settings.LANGSMITH_PROJECT in names
        print(f"{'✅' if ok else '⚠️'} 目标项目 {settings.LANGSMITH_PROJECT} "
              f"{'存在' if ok else '尚未创建（首次上报 trace 时自动创建）'}")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 项目列表获取失败：{exc}")


def verify_run(question: str) -> None:
    print("\n" + "=" * 60)
    print("② 真实提问 -> 全链路 trace 上云验证")
    print("=" * 60)
    from app.graph.main_graph import run_question

    result = run_question(question, thread_id="verify-langsmith", user_id="verify")
    stage = result.get("current_stage")
    answer = (result.get("final_answer") or "")[:200]
    print(f"✅ 主图执行完成 stage={stage}")
    print(f"   回答摘要: {answer}")
    if stage == "error":
        print(f"❌ 主图错误: {result.get('error_state')}")
        return
    runs = list_recent_runs(limit=5)
    print(f"   最近 trace 数: {len(runs)}")
    for r in runs:
        print(f"     - {r['name']}  {r['start_time'][:19]}  {r['url']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="LangSmith 接入验证")
    parser.add_argument("--run", type=int, help="1=额外跑一次真实提问验证 trace 上云")
    parser.add_argument("--question", default="6 月各平台广告花费是多少？", help="验证用提问")
    args = parser.parse_args()

    verify_client()
    if args.run:
        verify_run(args.question)
    print("\n完成。")


if __name__ == "__main__":
    main()
