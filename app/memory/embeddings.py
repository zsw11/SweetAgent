"""模拟 embedding（长期记忆 / 知识库向量）。

当前无真实向量模型可用（OpenAI text-embedding 未接入），使用**确定性伪向量**：
以文本为种子的字符 n-gram + 词双特征哈希桶 → L2 归一化。

性质：
- 相同文本 → 相同向量（可复现）
- 文本重叠度越高 → 向量越接近（近似"语义相关"，本质是关键词重叠检索）
- 与真实向量接口一致（list[float]，维度与 knowledge_chunks.embedding 一致）

将来接入真实 embedding 模型时，只需替换 mock_embedding 的实现，
表结构 / 检索逻辑 / 调用方零改动。
"""

from __future__ import annotations

import hashlib
import math

VECTOR_DIM = 1536  # 与 db/02-schema.sql knowledge_chunks.embedding vector(1536) 一致


def mock_embedding(text: str, dim: int = VECTOR_DIM) -> list[float]:
    """生成确定性伪向量（字符 3-gram 权重 1 + 词特征权重 3）。"""
    if not text:
        text = " "
    vec = [0.0] * dim
    t = text.lower()
    for i in range(max(0, len(t) - 2)):
        h = int(hashlib.md5(t[i:i + 3].encode("utf-8")).hexdigest(), 16) % dim
        vec[h] += 1.0
    for w in t.split():
        h = int(hashlib.md5(("w" + w).encode("utf-8")).hexdigest(), 16) % dim
        vec[h] += 3.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 6) for v in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度（未归一化输入也能算，内部做 L2 归一化）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def ngram_jaccard(a: str, b: str, n: int = 3) -> float:
    """字符 n-gram 集合 Jaccard 相似度（文本重叠度的直接度量）。

    用于记忆去重的补充判定：模拟向量对"包含关系"文本（A 是 B 的子串扩展）
    的余弦可能偏低，Jaccard 能稳定识别同主题。
    """
    def grams(s: str):
        s = (s or "").lower()
        return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def text_similarity(a: str, b: str) -> float:
    """综合文本相似度 = max(向量余弦, n-gram Jaccard)。"""
    if not a or not b:
        return 0.0
    return max(cosine_similarity(mock_embedding(a), mock_embedding(b)), ngram_jaccard(a, b))


def vector_to_sql(vec: list[float]) -> str:
    """list[float] → pgvector 字面量字符串（psycopg 未注册适配器时用文本转换）。"""
    return "[" + ",".join(str(round(v, 6)) for v in vec) + "]"


def vector_from_sql(raw) -> list[float]:
    """pgvector 返回的文本/数组 → list[float]。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [float(v) for v in raw]
    s = str(raw).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    if not s.strip():
        return []
    return [float(x) for x in s.split(",") if x.strip()]
