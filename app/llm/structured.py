"""LLM 输出通道统一封装（OPT-04/05 统一入口，2026-09-25）。

三条通道 + 统一语义：
- invoke_structured：with_structured_output（Pydantic 约束输出，OPT-04）
- invoke_tool：bind_tools（Function Calling，OPT-05）
- invoke_text：普通文本通道（统一降级兜底）

设计原则：
1. 通道层不吞业务——只做"尝试原生通道"：成功返回结构化值，失败返回 None，
   降级策略由调用方决定（plan 失败回退 FALLBACK_REQ、记忆失败回退空、SQL 失败重调文本）；
2. 返回值一律 dict（Pydantic model_dump），下游不感知通道差异；
3. extract_json 统一 JSON 提取（markdown 剥壳 + 花括号截取），替代各处手写 _parse_*_json。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from pydantic import BaseModel

from app.observability.logging import get_logger

# markdown 代码块剥壳（```json ... ```）
_CODE_BLOCK_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _log(logger_name: str, event: str, **kwargs: Any) -> None:
    get_logger(logger_name or "llm").warning(event, **kwargs)


def invoke_structured(
    model,
    schema: type[BaseModel],
    messages: list[Any],
    *,
    logger_name: str = "llm",
) -> Optional[dict[str, Any]]:
    """结构化输出通道（OPT-04）：with_structured_output → Pydantic dict。

    显式 method="function_calling"：langchain-openai 1.6+ 默认 json_schema（response_format），
    而 DeepSeek 兼容协议不支持 json_schema（400），function_calling（tools 强制）已验证可用。
    模型不支持 / 调用异常 / 返回 None 时返回 None，由调用方决定文本降级。
    """
    try:
        obj = model.with_structured_output(schema, method="function_calling").invoke(messages)
    except Exception as exc:
        _log(logger_name, "llm.structured.fallback", error=str(exc)[:200])
        return None
    if obj is None:
        return None
    return obj.model_dump() if hasattr(obj, "model_dump") else dict(obj)


def invoke_tool(
    model,
    tool_schema: dict[str, Any],
    messages: list[Any],
    arg_key: str,
    *,
    logger_name: str = "llm",
) -> tuple[Optional[str], Optional[str]]:
    """Function Calling 通道（OPT-05）：从 tool_calls 提取工具参数值。

    返回 (value, text)：
    - 工具通道成功        → (参数值, None)
    - 模型未走 tool_calls → (None, 原始文本回复)（可直接用，无需重调）
    - API 异常            → (None, None)（调用方需重调文本通道）
    """
    try:
        resp = model.bind_tools([tool_schema]).invoke(messages)
    except Exception as exc:
        _log(logger_name, "llm.invoke_tool.fallback",
             tool=tool_schema["function"]["name"], error=str(exc)[:200])
        return None, None
    tc = getattr(resp, "tool_calls", None) or []
    args = tc[0].get("args") or {} if tc else {}
    if args.get(arg_key):
        return str(args[arg_key]).strip(), None
    content = getattr(resp, "content", None)
    return None, str(content).strip() if content is not None else None


def invoke_text(
    model,
    messages: list[Any],
    *,
    logger_name: str = "llm",
) -> Optional[str]:
    """普通文本通道（统一降级兜底）：失败返回 None。"""
    try:
        resp = model.invoke(messages)
        return str(resp.content)
    except Exception as exc:
        _log(logger_name, "llm.text.fallback", error=str(exc)[:200])
        return None


def extract_json(text: str) -> Optional[dict[str, Any]]:
    """从 LLM 输出中提取 JSON dict（容忍 markdown 代码块 / 前后杂文）。"""
    t = (text or "").strip()
    m = _CODE_BLOCK_RE.match(t)
    if m:
        t = m.group(1).strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(t[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
