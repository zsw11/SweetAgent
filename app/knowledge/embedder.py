"""向量化：统一 embedding 接口（真实模型 / 模拟向量一键切换）。

选择规则（按优先级）：
1. OPENAI_API_KEY 有效 → 真实 embedding 模型（settings.EMBEDDING_MODEL，
   默认 text-embedding-3-small，1536 维，与表结构一致）；
2. 否则 → mock_embedding（确定性哈希伪向量，维度同样 1536），
   保证无 Key / Key 无效时全链路可跑、可复现（相同文本=相同向量、重叠文本=接近）。

降级设计：
- 静态启发式：占位 / 过短的 Key（如 "sk-xxx"）直接走 mock，零 API 调用；
- 动态兜底：真实模型调用失败（401 / 网络 / 限流）→ 本进程内永久降级 mock，
  避免每篇文档都重复失败重试（失败一次后零成本）。

将来换任何 embedding 提供方，只需实现一个 `embed_batch(texts) -> list[list[float]]`
并接入 `get_embedder()`，表结构 / 检索逻辑 / 调用方零改动。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Callable

from app.config.settings import settings
from app.memory.embeddings import mock_embedding
from app.observability.logging import get_logger

logger = get_logger("knowledge_embedder")

# mock 模型名（与 scripts/reindex_knowledge_embeddings.py 保持一致，可追溯）
MOCK_MODEL_NAME = "seed_random_mock"

# 真实模型失败后置 True，本进程内不再尝试（避免反复打无效 API）
_MOCK_ONLY = False


def _key_looks_valid(key: str) -> bool:
    """启发式判断 Key 是否值得尝试：非空、非占位、长度达标。"""
    if not key or len(key) < 20:
        return False
    if key.strip() == "sk-xxx" or key.strip().startswith("sk-xxx"):
        return False
    return True


def _mock_batch(texts: list[str]) -> list[list[float]]:
    return [mock_embedding(t or " ") for t in texts]


def _real_embed_batch(texts: list[str]) -> list[list[float]]:
    """OpenAI 兼容 embedding 接口批量向量化（失败自动降级 mock）。"""
    global _MOCK_ONLY
    if _MOCK_ONLY:
        return _mock_batch(texts)
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL or None,
        )
        # 空文本占位（部分提供方拒绝空串），与 mock 行为对齐
        texts = [t or " " for t in texts]
        resp = client.embeddings.create(model=settings.EMBEDDING_MODEL, input=texts)
        # 按输入顺序重排（OpenAI 返回顺序与输入一致，保险起见按 index 排）
        ordered = sorted(resp.data, key=lambda d: d.index)
        return [list(d.embedding) for d in ordered]
    except Exception as exc:
        _MOCK_ONLY = True
        logger.warning("knowledge.embedder.fallback_mock", error=str(exc)[:300])
        return _mock_batch(texts)


@lru_cache(maxsize=1)
def get_embedder() -> Callable[[list[str]], list[list[float]]]:
    """返回批量向量化函数（进程内缓存，避免反复探测 Key）。"""
    if _key_looks_valid(settings.OPENAI_API_KEY):
        logger.info("knowledge.embedder.provider", provider="openai", model=settings.EMBEDDING_MODEL)
        return _real_embed_batch
    logger.info("knowledge.embedder.provider", provider="mock", model=MOCK_MODEL_NAME)
    return _mock_batch


def embed_batch(texts: list[str]) -> list[list[float]]:
    """批量向量化（调用方统一入口）。"""
    return get_embedder()(texts)


def embed_query(text: str) -> list[float]:
    """单条 query 向量化。"""
    return embed_batch([text])[0]


def current_embedding_model() -> str:
    """当前生效的向量模型名（写库时记录，便于追溯/重灌）。"""
    return settings.EMBEDDING_MODEL if _key_looks_valid(settings.OPENAI_API_KEY) and not _MOCK_ONLY else MOCK_MODEL_NAME


def current_vector_dim() -> int:
    """当前向量维度（真实模型按配置；mock 固定 1536）。"""
    return settings.VECTOR_DIM if _key_looks_valid(settings.OPENAI_API_KEY) and not _MOCK_ONLY else len(mock_embedding(" "))
