"""Decision Agent 主逻辑（设计文档 15-16 节）。

接收各部门 Result，做事实整合 -> 交叉验证 -> 冲突检测 -> 原因归因 ->
方案制定 -> 优先级排序，输出结构化 DecisionOutput（供 Web UI 渲染）。

Decision Agent 不查数据库，是纯 LLM 综合分析节点。使用强推理模型（设计文档 55 节）。
"""

from __future__ import annotations

import json
from typing import Any, Optional, Union

from pydantic import BaseModel, Field

from app.agents.decision.output import DecisionOutput
from app.agents.decision.prompts import DECISION_PROMPT, DECISION_SYSTEM_PROMPT
from app.agents.decision.state import DecisionState
from app.config.settings import settings
from app.llm import get_chat_model, llm_available
from app.llm.structured import extract_json, invoke_structured, invoke_text
from app.observability.logging import get_logger

logger = get_logger("decision_agent")


# ---------------------------------------------------------------------------
# OPT-04 结构化输出：Pydantic 约束（with_structured_output 用），替代手写 JSON 容错
# ---------------------------------------------------------------------------

class DecisionFinding(BaseModel):
    category: str = "cross"
    finding: str = ""


class DecisionRootCause(BaseModel):
    cause: str = ""
    evidence: str = ""


class DecisionRecommendation(BaseModel):
    priority: str = "P1"
    action: str = ""


class DecisionRisk(BaseModel):
    risk: str = ""
    severity: str = "medium"


class DecisionOutputSchema(BaseModel):
    """Decision 输出 schema（与 DecisionOutput TypedDict 同构，字段全默认值）。"""
    summary: str = ""
    findings: list[DecisionFinding] = Field(default_factory=list)
    root_causes: list[DecisionRootCause] = Field(default_factory=list)
    recommendations: list[DecisionRecommendation] = Field(default_factory=list)
    risks: list[DecisionRisk] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class DecisionAgent:
    """决策 Agent：跨部门结果汇总、因果分析、决策建议（纯 LLM 综合，不查数据）。"""

    def __init__(self, model=None):
        if not llm_available():
            raise RuntimeError(
                "未配置 LLM API Key。Decision Agent 需要 LLM 做综合分析，"
                "请在 .env 中配置 Key 后重试。"
            )
        # Decision 需要强推理模型（设计文档 55 节：Decision → 强模型）
        self.model = model if model is not None else get_chat_model(tier="strong")

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def run(
        self,
        user_question: str,
        department_results: dict[str, Any],
        memory: Optional[str] = None,
        feedback: Optional[str] = None,
        injection_warning: str = "",
    ) -> DecisionOutput:
        """执行一次决策分析，返回结构化 DecisionOutput。

        Args:
            user_question: 用户原始问题。
            department_results: 各部门结果字典，如 {"operation": {...}, "finance": {...}}。
            memory: 用户级记忆 JSON 文本（画像/偏好/通用记忆），
                回答"用户自身相关"问题（如负责哪个市场）时注入，无则 None。
            feedback: 质量门反馈（考点二十九）——上一次回答的偏差诊断或用户纠正意见，
                quality_gate 评估不合格后回炉重生成时注入，指导本次修正；无则 None。
            injection_warning: 入口注入检测命中的边界警告（OPT-06），非空时作为
                独立 System 消息注入，要求忽略用户输入中的指令性内容。
        """
        logger.info(
            "decision.run.start",
            question=user_question[:100],
            departments=list(department_results.keys()),
            has_memory=bool(memory),
            has_feedback=bool(feedback),
            injection_flagged=bool(injection_warning),
        )
        raw = self._synthesize(user_question, department_results, memory, feedback, injection_warning)
        report = raw if isinstance(raw, dict) else self._parse_and_validate(raw)
        logger.info(
            "decision.run.done",
            confidence=report.get("confidence"),
            findings=len(report.get("findings", [])),
            recommendations=len(report.get("recommendations", [])),
        )
        return report

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------
    def _synthesize(
        self,
        user_question: str,
        department_results: dict[str, Any],
        memory: Optional[str] = None,
        feedback: Optional[str] = None,
        injection_warning: str = "",
    ) -> Union[dict[str, Any], str]:
        """生成结构化决策报告。

        优先走 with_structured_output（OPT-04：Pydantic 约束输出，返回 dict）；
        模型不支持 / 调用异常时降级为原始文本（走 _parse_and_validate 旧解析链）。

        feedback 非空时在 prompt 中注入"反馈意见"段（考点二十九）：
        quality_gate 判不合格后回炉，本次生成必须针对反馈逐条修正。
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        # 精简部门结果：只保留决策需要的字段，避免上下文爆炸
        slim = self._slim_department_results(department_results)
        messages = [
            SystemMessage(content=DECISION_SYSTEM_PROMPT),
            HumanMessage(content=DECISION_PROMPT.format(
                user_question=user_question,
                memory=memory or "（无）",
                feedback=feedback or "（无）",
                department_results_json=json.dumps(slim, ensure_ascii=False, default=str),
            )),
        ]
        # OPT-06：注入检测命中时追加边界警告（系统级约束，独立消息不被用户输入稀释）
        if injection_warning:
            messages.append(SystemMessage(content=injection_warning))
        # OPT-04：结构化通道优先（统一封装）
        d = invoke_structured(self.model, DecisionOutputSchema, messages, logger_name="decision_agent")
        if d is not None:
            logger.info("decision.structured.ok", confidence=d.get("confidence"))
            return d
        # 降级：普通文本回复 → 旧解析链
        raw = invoke_text(self.model, messages, logger_name="decision_agent") or ""
        logger.debug("decision.synthesize.llm", raw=raw[:3000])
        return raw

    @staticmethod
    def _slim_department_results(department_results: dict[str, Any]) -> dict[str, Any]:
        """精简部门结果：保留 summary / metrics / anomalies / analysis / confidence，
        丢弃原始 observations / sql_history 等大体积字段（决策不需要原始 SQL 结果）。"""
        slim: dict[str, Any] = {}
        for name, result in department_results.items():
            if not isinstance(result, dict):
                slim[name] = result
                continue
            slim[name] = {
                "summary": result.get("summary", ""),
                "analysis": result.get("analysis", []),
                "metrics": result.get("metrics", []),
                "anomalies": result.get("anomalies", []),
                "findings": result.get("findings", []),
                "confidence": result.get("confidence", 0.0),
                "error": result.get("error"),
            }
        return slim

    def _parse_and_validate(self, raw: str) -> DecisionOutput:
        """解析 LLM 输出为结构化 DecisionOutput，缺失字段填默认值。"""
        parsed = _parse_decision_json(raw)
        if not parsed:
            logger.warning("decision.parse.failed", raw_preview=raw[:500])
            return _fallback_report(raw)

        # 校验并补全各字段
        report: DecisionOutput = {
            "summary": str(parsed.get("summary", "")).strip() or "未能生成核心结论（LLM 输出解析失败）。",
            "findings": _ensure_list_of_dicts(parsed.get("findings"), keys=("category", "finding")),
            "root_causes": _ensure_list_of_dicts(parsed.get("root_causes"), keys=("cause", "evidence")),
            "recommendations": _ensure_list_of_dicts(parsed.get("recommendations"), keys=("priority", "action")),
            "risks": _ensure_list_of_dicts(parsed.get("risks"), keys=("risk", "severity")),
            "confidence": _clamp_confidence(parsed.get("confidence")),
        }
        return report


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _parse_decision_json(text: str) -> Optional[dict[str, Any]]:
    """从 LLM 输出中提取 JSON（统一委托 extract_json：markdown 剥壳 + 花括号截取）。"""
    return extract_json(text)


def _ensure_list_of_dicts(value: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """确保输出字段是字典列表；非字典项跳过，缺失键补空字符串。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            out.append({k: item.get(k, "") for k in keys})
        elif isinstance(item, str):
            # LLM 可能输出字符串列表，包装成单字段字典
            out.append({keys[0]: item, **{k: "" for k in keys[1:]}})
    return out


def _clamp_confidence(value: Any) -> float:
    """将置信度限制在 0-1 范围；非数字返回 0.5。"""
    try:
        c = float(value)
        return max(0.0, min(1.0, c))
    except (TypeError, ValueError):
        return 0.5


def _fallback_report(raw: str) -> DecisionOutput:
    """LLM 输出无法解析时的降级报告（保留原文供追溯，不编造结论）。"""
    return {
        "summary": "决策分析生成失败：LLM 输出无法解析为结构化 JSON。请查看日志 decision.parse.failed。",
        "findings": [{"category": "system", "finding": "Decision Agent 输出解析失败，原始输出已记录到日志。"}],
        "root_causes": [],
        "recommendations": [{"priority": "P1", "action": "检查 LLM 服务状态与 Decision Prompt 版本，重试查询。"}],
        "risks": [{"risk": "无法提供结构化决策报告", "severity": "high"}],
        "confidence": 0.0,
    }


# 供 graph.py 复用的 state 构建辅助
def initial_state(
    user_question: str,
    department_results: Optional[dict[str, Any]] = None,
) -> DecisionState:
    """构造 DecisionState 初始值。"""
    return {
        "user_question": user_question,
        "department_results": department_results or {},
        "evidence": [],
        "conflicts": [],
        "root_causes": [],
        "recommendations": [],
        "confidence": 0.0,
        "final_report": None,
    }
