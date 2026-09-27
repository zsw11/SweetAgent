"""检索器：PGVector 相似度检索 + metadata filtering + 关键词兜底（设计文档 35-38 节）。

核心思路：
1. **向量召回**：query 向量化 → `embedding <=> %s::vector`（pgvector 余弦距离算子）→
   相似度 = 1 - 距离，按相似度倒序取 top-k；metadata 过滤（department/brand/market/
   document_type）在 SQL 层用 JOIN knowledge_documents 完成，而不是在代码里过滤——
   让数据库把最相关行压缩到 top-k，避免全表拉回应用层。
2. **多个关键词兜底（混合检索）**：当向量检索最高分低于阈值（mock 向量对完全无关文本
   也可能有哈希碰撞噪声分），回退到 ILIKE 关键词检索，保证"知识库里明明有相关内容
   却因向量质量召回不到"时不至于漏检。
3. **只读执行**：复用 ReadOnlyExecutor（agent_reader 角色，仅 SELECT）。

为什么 JOIN documents 而不是用 chunks.metadata：
- department/brand/market 在 documents 上是权威字段（单一事实来源），
  chunks.metadata 只是冗余快照；JOIN 一次同时拿到标题/来源类型，供注入展示。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from app.cache.ttl_cache import get_rag_cache
from app.config.settings import settings
from app.knowledge.embedder import current_embedding_model, embed_query
from app.memory.embeddings import vector_to_sql
from app.observability.logging import get_logger
from app.tools.sql.executor import ReadOnlyExecutor

logger = get_logger("knowledge_retriever")

# mock 向量下完全无关文本也可能有哈希碰撞噪声分；低于此值视为"向量召回不可信"
DEFAULT_MIN_SCORE = 0.20
DEFAULT_TOP_K = 5
KEYWORD_TOP_K = 3   # 关键词兜底返回条数（比向量少，避免稀释上下文）

_EN_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_CN_SEG_RE = re.compile(r"[\u4e00-\u9fff]+")
# 无实义的中文 2-gram / 词（停用）
_STOP_WORDS = {
    "什么", "怎么", "如何", "为什么", "一个", "多少", "需要", "以及", "或者", "但是",
    "如果", "因为", "所以", "这个", "那个", "还有", "进行", "一下", "是否", "没有",
    "我们", "你们", "他们", "可以", "应该", "请问", "想要", "知道", "了解", "分析",
    "and", "the", "for", "what", "why", "how", "is", "are", "of", "in", "to",
}
# 单字虚词：2-gram 中任一字为虚词 → 跨词边界垃圾（如「货与」「与预」「于多」），
# 它们不构成独立概念且几乎匹配不到，却会挤占关键词名额，直接过滤。
_VIRTUAL_CHARS = set("与于在的了是也和或及为从对把被吗呢啊这那之而并且")

def _extract_keywords(text: str, max_keys: int = 8) -> list[str]:
    """query → 检索关键词：英文/数字连续词 + 中文 2-gram，去重去停用词。

    注意：不能用 [\\w]+ 直接切——\\w 在 Python 中匹配中文，会把整句中文当成一个词，
    ILIKE 全串匹配必然落空；中文按 2-gram 切分能捕捉「美国/床垫/市场」等关键概念；
    含虚词的跨界 2-gram（如「货与」「与预」）直接过滤，避免挤占检索名额。
    """
    words: list[str] = []
    words.extend(_EN_WORD_RE.findall(text or ""))
    for seg in _CN_SEG_RE.findall(text or ""):
        for i in range(len(seg) - 1):
            gram = seg[i:i + 2]
            if gram[0] in _VIRTUAL_CHARS or gram[1] in _VIRTUAL_CHARS:
                continue  # 跨界垃圾 2-gram（跨了虚词边界），不构成检索词
            words.append(gram)
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        w = w.lower()
        if len(w) < 2 or w in _STOP_WORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:max_keys]


def _evidence_confidence(similarity: float, hit_count: Optional[int], method: str) -> str:
    """单条命中的证据置信度分档（供下游 LLM 判断引用强度）。

    - vector 命中按相似度：≥0.60 high / 0.30~0.60 medium / 0.20~0.30 low；
      低于 0.20 的向量结果不可信，本就走关键词兜底，不会作为 vector 返回。
    - keyword 命中按命中词数：≥3 high / 2 medium / 1 low（只命中 1 个词≈字面巧合）。
    """
    if method == "vector":
        if similarity >= 0.60:
            return "high"
        if similarity >= 0.30:
            return "medium"
        return "low"
    if hit_count is None or hit_count >= 3:
        return "high"
    return "medium" if hit_count == 2 else "low"


class KnowledgeRetriever:
    """企业知识库检索器（向量 + 关键词混合）。

    OPT-07（2026-09-27）检索缓存：
    - 键 = query + 全部 metadata 过滤 + top_k + min_score + embedding 模型；
      embedding 模型进键是关键——mock 与真实模型向量语义不同，模型切换必须自动 miss；
    - TTL = settings.RAG_CACHE_TTL_SECONDS（长 TTL，知识库低频变更）；
    - 失效：知识库重灌后调用 invalidate()（全清最简，见 app.knowledge.ingest）；
    - 缓存挂模块级共享实例：每个 KnowledgeRetriever 实例共享同一份缓存。
    """

    def __init__(
        self,
        executor: Optional[ReadOnlyExecutor] = None,
        top_k: int = DEFAULT_TOP_K,
        min_score: float = DEFAULT_MIN_SCORE,
    ):
        self.executor = executor or ReadOnlyExecutor()
        self.top_k = top_k
        self.min_score = min_score

    @staticmethod
    def invalidate() -> int:
        """知识库内容变更后清空 RAG 检索缓存（幂等，可反复调用）。"""
        return get_rag_cache().clear()

    @staticmethod
    def _cache_key(
        query: str,
        *,
        department: Optional[str],
        brand: Optional[str],
        market: Optional[str],
        document_type: Optional[str],
        top_k: int,
        min_score: float,
    ) -> Optional[str]:
        """检索缓存键：query + 全部过滤条件 + 参数 + embedding 模型指纹。"""
        if not settings.CACHE_ENABLED:
            return None
        parts = [
            (query or "").strip(),
            department or "", brand or "", market or "", document_type or "",
            str(top_k), str(min_score),
            current_embedding_model(),  # 向量模型变化 → 缓存自动失效
        ]
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
        return f"rag:{digest}"

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        department: Optional[str] = None,
        brand: Optional[str] = None,
        market: Optional[str] = None,
        document_type: Optional[str] = None,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """混合检索：向量召回为主，相似度不足时关键词兜底。

        Args:
            query: 检索问题（用户任务/问题原文）。
            department / brand / market / document_type: metadata 过滤（可选）。
            top_k: 返回条数（默认 self.top_k）。
            min_score: 向量检索最低可接受相似度，低于则触发关键词兜底。

        Returns:
            [{"chunk_id", "document_id", "chunk_index", "title", "source_type",
              "department", "brand", "market", "content", "similarity", "method",
              "confidence"}]
            method ∈ {"vector", "keyword"}，标识命中来源（可观测）。
        """
        top_k = top_k or self.top_k
        min_score = self.min_score if min_score is None else min_score
        cache_key = self._cache_key(
            query, department=department, brand=brand, market=market,
            document_type=document_type, top_k=top_k, min_score=min_score,
        )
        if cache_key is not None:
            cached = get_rag_cache().get(cache_key)
            if cached is not None:
                logger.debug("knowledge.retrieve.cache_hit", query=(query or "")[:60])
                return cached

        qvec = embed_query(query)
        hits = self._vector_search(qvec, query, department=department, brand=brand,
                                   market=market, document_type=document_type, top_k=top_k)
        hits = self._decorate(hits)
        # 向量召回不可信（最高分过低）→ 关键词兜底
        if not hits or hits[0]["similarity"] < min_score:
            kw = self._keyword_search(query, department=department, market=market,
                                      document_type=document_type, top_k=KEYWORD_TOP_K)
            if kw:
                logger.info(
                    "knowledge.retrieve.fallback",
                    method="keyword", reason="low_similarity", query=query,
                    best_score=round(hits[0]["similarity"], 3) if hits else None,
                )
                hits = self._decorate(kw)
            elif hits:
                # 双通道都不可信：向量分过低、关键词也零命中 → 承认"无合适检索"，
                # 返回空让下游显式回答"知识库未收录"，而不是硬塞低分噪声结果。
                logger.info(
                    "knowledge.retrieve.none",
                    reason="both_channels_unreliable", query=query,
                    best_score=round(hits[0]["similarity"], 3),
                )
                return []
        result = [self._stamp_confidence(h) for h in hits]
        if cache_key is not None:
            get_rag_cache().set(cache_key, result, settings.RAG_CACHE_TTL_SECONDS)
        return result

    @staticmethod
    def _stamp_confidence(hit: dict[str, Any]) -> dict[str, Any]:
        """给命中结果打证据置信度标记（high/medium/low），供下游引用强度判断。"""
        return hit | {
            "confidence": _evidence_confidence(
                hit["similarity"], hit.get("hit_count"), hit.get("method", "vector")
            )
        }

    # ------------------------------------------------------------------
    # 向量检索
    # ------------------------------------------------------------------
    def _vector_search(
        self,
        qvec: list[float],
        query: str,
        *,
        department: Optional[str],
        brand: Optional[str],
        market: Optional[str],
        document_type: Optional[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        where, params = ["c.embedding IS NOT NULL", "d.status = 'active'"], []
        if department:
            where.append("d.department = %s")
            params.append(department)
        if brand:
            where.append("d.brand = %s")
            params.append(brand)
        if market:
            where.append("d.market = %s")
            params.append(market)
        if document_type:
            where.append("d.source_type = %s")
            params.append(document_type)
        # `c.embedding <= > % s::vector
        # ` 算出的是 ** 向量距离 **：值越小 = 两个向量方向越接近 = 越相关。范围约[0, 2]（完全同向 ≈ 0，正交 ≈ 1，完全反向 ≈ 2）。
        sql = (
            "SELECT c.id AS chunk_id, c.document_id, c.chunk_index, c.content, "
            "d.title, d.source_type, d.department, d.brand, d.market, "
            "1 - (c.embedding <=> %s::vector) AS similarity "
            "FROM knowledge_chunks c "
            "JOIN knowledge_documents d ON d.id = c.document_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY c.embedding <=> %s::vector "
            "LIMIT %s"
        )
        params = [vector_to_sql(qvec), *params, vector_to_sql(qvec), top_k]
        try:
            result = self.executor.execute(sql, tuple(params))
        except Exception as exc:
            logger.warning("knowledge.retrieve.vector_fail", error=str(exc), query=query)
            return []
        return [self._normalize(r) | {"method": "vector"} for r in result["rows"]]

    # ------------------------------------------------------------------
    # 关键词兜底检索
    # ------------------------------------------------------------------
    def _keyword_search(
        self,
        query: str,
        *,
        department: Optional[str],
        market: Optional[str],
        document_type: Optional[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        keys = _extract_keywords(query)
        if not keys:
            return []
        # OR 匹配任一关键词；ORDER BY 命中关键词数降序（相关度优先，再按 id）
        or_conds = ["c.content ILIKE %s"] * len(keys)
        hit_expr = " + ".join(["(c.content ILIKE %s)::int"] * len(keys))
        hit_params = [f"%{k}%" for k in keys]
        params: list = hit_params + hit_params  # 命中数表达式 + OR 条件各一份，顺序严格对应
        filters: list[str] = []
        if department:
            filters.append("d.department = %s")
            params.append(department)
        if market:
            filters.append("d.market = %s")
            params.append(market)
        if document_type:
            filters.append("d.source_type = %s")
            params.append(document_type)

        sql = (
            "SELECT c.id AS chunk_id, c.document_id, c.chunk_index, c.content, "
            "d.title, d.source_type, d.department, d.brand, d.market, "
            f"{hit_expr} AS hit_count "
            "FROM knowledge_chunks c "
            "JOIN knowledge_documents d ON d.id = c.document_id "
            f"WHERE d.status = 'active' AND ({' OR '.join(or_conds)})"
            + (f" AND {' AND '.join(filters)}" if filters else "")
            + " ORDER BY hit_count DESC, c.id LIMIT %s"
        )
        params.append(top_k)
        try:
            result = self.executor.execute(sql, tuple(params))
        except Exception as exc:
            logger.warning("knowledge.retrieve.keyword_fail", error=str(exc), query=query)
            return []
        return [self._normalize(r) | {"method": "keyword"} for r in result["rows"]]

    # ------------------------------------------------------------------
    # 数据规整
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize(row: dict[str, Any]) -> dict[str, Any]:
        """行 → 统一 dict。向量行有 similarity 列；关键词行有 hit_count 列（相似度语义=1.0 高可信）。"""
        return {
            "chunk_id": row["chunk_id"],
            "document_id": row["document_id"],
            "chunk_index": row["chunk_index"],
            "title": row["title"],
            "source_type": row["source_type"],
            "department": row["department"],
            "brand": row["brand"],
            "market": row["market"],
            "content": row["content"],
            "similarity": round(float(row.get("similarity") or (1.0 if "hit_count" in row else 0.0)), 4),
            "hit_count": row.get("hit_count"),
        }

    @staticmethod
    def _decorate(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按相似度降序（关键词兜底相似度恒定 1.0，排最前）。"""
        return sorted(hits, key=lambda h: h["similarity"], reverse=True)


def search_knowledge(
    query: str,
    *,
    department: Optional[str] = None,
    brand: Optional[str] = None,
    market: Optional[str] = None,
    document_type: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    min_score: Optional[float] = None,
) -> list[dict[str, Any]]:
    """便捷入口：单例检索器。"""
    _retriever: Optional[KnowledgeRetriever] = None

    def _get() -> KnowledgeRetriever:
        nonlocal _retriever
        if _retriever is None:
            _retriever = KnowledgeRetriever(top_k=top_k, min_score=min_score or DEFAULT_MIN_SCORE)
        return _retriever

    return _get().search(
        query,
        department=department,
        brand=brand,
        market=market,
        document_type=document_type,
        top_k=top_k,
        min_score=min_score,
    )


def invalidate_knowledge_cache() -> int:
    """知识库变更后统一失效缓存（RAG 全清 + SQL 涉及 knowledge_* 表的结果缓存）。"""
    n = KnowledgeRetriever.invalidate()
    for table in ("knowledge_documents", "knowledge_chunks", "knowledge_embeddings"):
        n += ReadOnlyExecutor.invalidate_table(table)
    return n
