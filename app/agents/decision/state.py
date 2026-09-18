"""Decision Agent State（设计文档 15 节）。"""

from typing import Any, TypedDict


class DecisionState(TypedDict, total=False):
    user_question: str
    department_results: dict[str, Any]
    evidence: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    root_causes: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]
    confidence: float
    final_report: dict[str, Any]
