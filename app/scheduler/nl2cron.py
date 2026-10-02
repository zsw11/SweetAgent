"""NL2Cron：用户口述 → 结构化解析 → 模板匹配 → 三档置信分支（考点六十四）。

流程：
1. 意图识别（调用方/API 层判断是否创建定时任务，复用 Manager 意图能力）
2. LLM 结构化解析：invoke_structured 抽 {capability, time_expr, params, question_desc}
3. 确定性校验闸：能力域白名单 + 时间解析（频率上限）+ 参数 Schema + 风险分级
4. 三档置信分支：high 直接创建 / mid 对话澄清 / low 展示目录

返回统一结构化结果，供 API 层决定后续动作（创建/反问/展示目录）。
"""

from __future__ import annotations

from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.config.settings import settings
from app.llm import get_chat_model
from app.llm.structured import invoke_structured
from app.observability.logging import get_logger
from app.scheduler import time_parser
from app.scheduler.registry import get_domain, list_domains
from app.scheduler.risk import evaluate_risk

logger = get_logger("scheduler.nl2cron")

# 置信度阈值（与设计一致：≥0.8 高、0.5~0.8 中、<0.5 低）
CONF_HIGH = settings.SCHEDULER_CONFIDENCE_HIGH
CONF_MID = settings.SCHEDULER_CONFIDENCE_MID


class NL2CronOutput(BaseModel):
    """LLM 结构化解析输出（只做语义抽取，不做任务发明）。"""
    capability: str = Field("", description="能力域 key，只能从提供列表里选；不确定留空")
    time_expr: str = Field("", description="自然语言时间表达，如'每天早上9点'")
    params: dict[str, Any] = Field(default_factory=dict, description="业务参数（对应能力域的字段）")
    question_desc: str = Field("", description="一句话复述用户意图（回显/澄清用）")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="LLM 对能力域匹配的置信度")


def _build_system_prompt() -> str:
    """构造解析 prompt：列出能力域目录 + 约束（只能选白名单）。"""
    catalog = "\n".join(
        f"- {d['key']}（{d['name']}）：{d['description']}；示例：{('；'.join(d['examples']))}"
        for d in list_domains()
    )
    return (
        "你是定时任务意图解析器。用户想创建定时任务，请把用户口述解析成结构化字段。\n"
        "硬性约束：\n"
        "1. capability 只能从下面目录里选一个 key；用户需求不在目录内 → capability 留空、confidence=0；\n"
        "2. 绝不自行发明新任务类型、不生成 cron 表达式、不写任何可执行逻辑；\n"
        "3. time_expr 保留用户的自然语言时间说法（如'每天早上9点'），不要转 cron；\n"
        "4. params 只填与所选能力域匹配的业务字段（问题/指标/阈值等）；\n"
        "5. confidence 填你对'用户意图落在所选能力域'的把握（0~1）。\n\n"
        f"可用能力域目录：\n{catalog}"
    )


def parse_nl(text: str, model=None) -> Optional[dict[str, Any]]:
    """LLM 结构化解析用户口述。失败返回 None（调用方走降级）。"""
    m = model or get_chat_model(tier="small")
    messages = [
        SystemMessage(content=_build_system_prompt()),
        HumanMessage(content=f"用户口述：{text}"),
    ]
    return invoke_structured(m, NL2CronOutput, messages, logger_name="scheduler.nl2cron")


def validate_parse(parsed: dict[str, Any]) -> dict[str, Any]:
    """确定性校验闸：白名单 + 时间 + 参数 Schema + 风险分级。

    返回 {"valid": bool, "reason": str, "cron": str, "risk": str, ...}
    """
    capability = (parsed.get("capability") or "").strip()
    time_expr = (parsed.get("time_expr") or "").strip()
    params = parsed.get("params") or {}
    confidence = float(parsed.get("confidence") or 0.0)

    # ① 能力域白名单
    domain = get_domain(capability)
    if domain is None:
        return {"valid": False, "reason": f"unknown_capability:{capability or '(空)'}", "confidence": confidence}

    # ② 时间解析 + 频率上限
    cron = time_parser.parse_time_expr(time_expr)
    if cron is None:
        return {
            "valid": False,
            "reason": "时间表达无法解析或触发频率过低（最小间隔限制）",
            "confidence": confidence,
        }

    # ③ 参数 Schema 校验
    try:
        model = domain.params_schema(**params)
        clean_params = model.model_dump()
    except Exception as exc:
        return {"valid": False, "reason": f"params_schema:{str(exc)[:200]}", "confidence": confidence}

    # ④ 风险分级
    risk = evaluate_risk(capability, clean_params)

    return {
        "valid": True,
        "capability": capability,
        "domain_name": domain.name,
        "time_expr": time_expr,
        "cron": cron,
        "cron_human": time_parser.format_cron_human(cron),
        "params": clean_params,
        "risk": risk,
        "confidence": confidence,
    }


def route(parsed: dict[str, Any]) -> dict[str, Any]:
    """三档置信分支：high 直接可创建 / mid 需澄清 / low 展示目录。

    Returns: {"branch": "create"|"clarify"|"catalog", ...}
    """
    confidence = float(parsed.get("confidence") or 0.0)
    if confidence >= CONF_HIGH:
        return {"branch": "create"}
    if confidence >= CONF_MID:
        return {"branch": "clarify"}
    return {"branch": "catalog"}
