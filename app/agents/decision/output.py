"""Decision Agent 输出标准（设计文档 16 节）。

最终输出结构化 JSON，由 Web UI 渲染为：核心结论 -> 关键数据 -> 原因分析 -> 证据 -> 建议 -> 风险。
"""

from typing import Any, TypedDict


class DecisionOutput(TypedDict, total=False):
    summary: str
    findings: list[dict[str, Any]]      # [{"category": "finance", "finding": "..."}]
    root_causes: list[dict[str, Any]]   # [{"cause": "...", "evidence": "..."}]
    recommendations: list[dict[str, Any]]  # [{"priority": "P0", "action": "..."}]
    risks: list[dict[str, Any]]
    confidence: float
