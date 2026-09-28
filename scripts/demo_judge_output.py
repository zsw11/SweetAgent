# -*- coding: utf-8 -*-
"""demo：judge_memory 的输入/输出长什么样（块C/记忆裁判面试演示）。

用法：.venv\\Scripts\\python.exe scripts\\demo_judge_output.py
1) 打印 JUDGE_PROMPT 实际渲染的输入（新记忆 + 候选）
2) 真实调用 judge_memory（DeepSeek small tier），打印 d = invoke_structured(...) 的返回
3) 展示 JudgeOutput.model_dump() 四类关系输出的形态
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.memory.judge import JUDGE_PROMPT, JudgeOutput, judge_memory  # noqa: E402

# 项目风格的真实候选（模拟 add_memory 里 semantic.py 122-124 行构造的 top-5）
CANDIDATES = [
    {"id": 131, "memory_type": "fact", "content": "美国床垫9月销量下滑26%", "confidence": 0.80},
    {"id": 132, "memory_type": "fact", "content": "SN-Q12-US 是畅销 SKU", "confidence": 0.70},
    {"id": 133, "memory_type": "conclusion", "content": "上次分析认为时效是退货主因", "confidence": 0.60},
    {"id": 134, "memory_type": "fact", "content": "退货率环比上升2%", "confidence": 0.75},
    {"id": 135, "memory_type": "preference", "content": "报告默认用美元结算", "confidence": 0.90},
]


def show_input(new_content: str, new_type: str) -> None:
    """展示 JUDGE_PROMPT 渲染后的输入形态（新记忆 1 条 + 候选 5 条）。"""
    cand_lines = [
        f"[{c['id']}] type={c.get('memory_type') or '?'}, confidence={c.get('confidence')}, content={c['content'][:200]}"
        for c in CANDIDATES
    ]
    prompt = JUDGE_PROMPT.format(
        new=new_content[:1000],
        new_type=new_type or "?",
        new_conf="None",
        candidates="\n".join(cand_lines)[:3000],
    )
    print("=" * 70)
    print("[1] 输入：渲染后的 JUDGE_PROMPT（截取到候选列表为止）")
    print("=" * 70)
    for line in prompt.splitlines()[:11]:
        print(f"  {line}")


def main() -> None:
    new_content = "美国床垫9月销量回升5%"
    show_input(new_content, "fact")

    print("=" * 70)
    print("[2] 真实调用 judge_memory → d（invoke_structured 返回）")
    print("=" * 70)
    d = judge_memory(new_content, "fact", CANDIDATES, new_confidence=0.95)
    if d is None:
        print("  （LLM 不可用或输出解析失败 → None，add_memory 会走降级阈值逻辑）")
    else:
        print(f"  d = {d!r}")

    print("=" * 70)
    print("[3] JudgeOutput.model_dump() 四类关系输出形态（确定性示例）")
    print("=" * 70)
    demos = [
        JudgeOutput(relation="unrelated", target_id=None, event="ADD", new_content="",
                    reason="新记忆与所有候选都无关，直接新增"),
        JudgeOutput(relation="duplicate", target_id=132, event="NONE", new_content="",
                    reason="与候选132是同一事实的换说法，仅刷新 evidence/confidence"),
        JudgeOutput(relation="supplement", target_id=133, event="MERGE",
                    new_content="时效与物流共同导致退货（含时效与海外仓数据）",
                    reason="新记忆是对候选133的补充细化，旧候选作废写入合并内容"),
        JudgeOutput(relation="conflict", target_id=131, event="UPDATE", new_content="",
                    reason="与候选131矛盾（下滑26% vs 回升5%），新表达置信度更高，版本化更新"),
    ]
    for x in demos:
        print(f"  {x.model_dump()!r}")


if __name__ == "__main__":
    main()
