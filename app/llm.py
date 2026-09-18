"""统一 LLM 工厂（设计文档 44-45 节）。

职责：
- 按 provider（deepseek / openai）创建 ChatOpenAI 实例（OpenAI 兼容协议）
- 检测 API Key 是否真实可用：占位符 / 空值 / 过短视为"未配置"
- llm_available() 供上层判断 Key 是否可用（未配置时 Operation Agent 启动即报错）
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from langchain_openai import ChatOpenAI

from app.config.settings import settings

# 常见占位符 key（.env.example 默认值 / 用户手填的示例）
_PLACEHOLDER_KEYS = {"sk-xxx", "your-key", "sk-your-key", "your_api_key", "xxx"}
_MIN_KEY_LENGTH = 20


def _real_key(key: str) -> bool:
    """判断 key 是否真实可用：非空、非占位、长度达到真实 key 下限。"""
    k = (key or "").strip()
    if not k or k.lower() in _PLACEHOLDER_KEYS or k.startswith("sk-xxx"):
        return False
    return len(k) >= _MIN_KEY_LENGTH


def llm_available(provider: Optional[str] = None) -> bool:
    """当前 provider 是否配置了可用 key。"""
    provider = provider or settings.LLM_DEFAULT_PROVIDER
    if provider == "openai":
        return _real_key(settings.OPENAI_API_KEY)
    return _real_key(settings.DEEPSEEK_API_KEY)


def get_chat_model(
    provider: Optional[str] = None,
    tier: str = "medium",
    temperature: float = 0.0,
) -> ChatOpenAI:
    """创建 ChatOpenAI 实例（OpenAI 兼容协议，deepseek 同协议）。

    Args:
        provider: "deepseek" / "openai"，默认取配置 LLM_DEFAULT_PROVIDER。
        tier: strong / medium / small，对应各 provider 的模型配置。
        temperature: 生成温度；SQL 生成建议 0。
    """
    provider = provider or settings.LLM_DEFAULT_PROVIDER

    if provider == "openai":
        base_url = settings.OPENAI_BASE_URL
        api_key = settings.OPENAI_API_KEY
        model = getattr(settings, f"OPENAI_MODEL_{tier.upper()}")
    else:
        base_url = settings.DEEPSEEK_BASE_URL
        api_key = settings.DEEPSEEK_API_KEY
        model = getattr(settings, f"DEEPSEEK_MODEL_{tier.upper()}")

    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
        timeout=60,
        max_retries=2,
    )


@lru_cache
def get_cached_chat_model(
    provider: Optional[str] = None,
    tier: str = "medium",
) -> ChatOpenAI:
    """进程内缓存的模型实例（避免每次调用重复建连接）。"""
    return get_chat_model(provider=provider, tier=tier)
