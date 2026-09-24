"""质量自纠回路：quality_gate 的评估逻辑（考点二十九，2026-09-23）。

职责：判断 Decision 输出的答案是否"回答到点上"：
1. 规则检查（确定性、零成本，永远启用）——空/过短、降级失败态、模板甩锅话术；
2. LLM 裁判（可选，settings.QUALITY_GATE_JUDGE_ENABLED）——跑题/漏答语义检查，
   与评估体系（考点二十三）同一哲学：确定性优先、LLM 只兜底语义判断。

不合格时由 main_graph 的 quality_gate 节点决定：未达上限自动回炉（带 feedback），
耗尽后 interrupt() 等用户纠正（human-in-the-loop）或放行。
"""

from __future__ import annotations

import json
from typing import Any

from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("quality_gate")

# LLM 常见"甩锅/没答到点上"的模板话术（命中即视为无效回答，零成本规则）
_WEAK_PATTERNS = (
    "请提供更多信息",
    "需要更多数据",
    "无法回答",
    "我无法",
    "暂无法",
    "请补充",
    "需要进一步",
    "请确认",
)

# 降级/失败态前缀（Decision 异常兜底输出的 summary 开头）
_FAILED_PREFIXES = (
    "决策分析生成失败",
    "Decision Agent 执行失败",
    "决策分析无法",
)


def check_answer_quality(
    user_question: str,
    decision_result: dict[str, Any],
) -> dict[str, Any]:
    """规则检查（确定性、零成本）：返回 {"pass": bool, "issues": [str, ...]}。

    检查维度：
    1. 核心结论为空或过短（< QUALITY_GATE_MIN_SUMMARY_LEN）——未形成有效回答；
    2. 降级/失败态——Decision 异常兜底输出，需重新生成；
    3. 模板甩锅话术——LLM 没答到点上时的典型措辞。
    """
    summary = str((decision_result or {}).get("summary", "") or "").strip()
    issues: list[str] = []

    if len(summary) < settings.QUALITY_GATE_MIN_SUMMARY_LEN:
        issues.append(
            f"核心结论为空或过短（{len(summary)} 字 < {settings.QUALITY_GATE_MIN_SUMMARY_LEN} 字），未形成有效回答"
        )
    elif decision_result.get("error") or summary.startswith(_FAILED_PREFIXES):
        issues.append("当前答案为降级/失败态（Decision 异常兜底输出），需要重新生成")
    else:
        for pat in _WEAK_PATTERNS:
            if pat in summary:
                issues.append(f"核心结论疑似未回答到点上（命中模板话术「{pat}」）")
                break

    if issues:
        logger.info("quality_gate.rule.fail", n_issues=len(issues), issues=issues[:3])
    else:
        logger.debug("quality_gate.rule.pass", summary_len=len(summary))

    return {"pass": not issues, "issues": issues}


def llm_quality_check(user_question: str, answer: str) -> list[str]:
    """LLM 裁判：判断回答是否覆盖问题要点（跑题/漏答检测）。

    仅在 settings.QUALITY_GATE_JUDGE_ENABLED=True 时由节点调用；
    使用 tier=small 模型（temperature 默认 0），输出结构化为 {"pass": 0/1, "issues": [...]}。
    解析失败按"无问题"处理（裁判不可用时不得阻塞主流程）。
    """
    from app.llm import get_chat_model

    judge = get_chat_model(tier="small")
    prompt = (
        "判断以下「用户问题」与「Agent 回答」是否一致——回答是否正面覆盖了问题要点。\n\n"
        f"用户问题：{user_question}\n\n"
        f"Agent 回答：{answer[:4000]}\n\n"
        "只输出 JSON：\n"
        "- 回答覆盖问题要点 → {\"pass\": 1}\n"
        "- 回答跑题 / 漏答 / 答非所问 → {\"pass\": 0, \"issues\": [\"具体偏差1\", \"具体偏差2\"]}\n"
    )
    try:
        resp = judge.invoke(prompt)
        raw = str(resp.content if hasattr(resp, "content") else resp)
        start, end = raw.index("{"), raw.rindex("}") + 1
        verdict = json.loads(raw[start:end])
        if verdict.get("pass") == 0:
            issues = verdict.get("issues") or []
            logger.info("quality_gate.judge.fail", issues=issues[:3])
            return [str(i) for i in issues][:3]
        logger.debug("quality_gate.judge.pass")
        return []
    except Exception as exc:
        logger.warning("quality_gate.judge.failed", error=str(exc))
        return []
