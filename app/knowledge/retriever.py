"""检索器：RAG 三级流水线（查询优化 → 多路召回 → 重排序），PGVector + ILIKE 混合检索。

核心思路（2026-09-29 重构，替代旧"向量为主、相似度不足关键词兜底"模式）：
1. **查询优化**（第 1 阶段，`app/knowledge/query_optimizer.py`）：
   用户 query 先做 LLM 查询改写（补全指代/实体）+ HyDE（生成假想答案文档再向量化），
   产出多视角 query；LLM 不可用/失败自动降级为仅原 query（保持 mock 可跑）。
2. **多路召回**（第 2 阶段）：不再"分低才走关键词"，而是四路并行——
   原 query 向量 / 改写 query 向量 / HyDE 向量（cosine，pgvector）+ 关键词 ILIKE，
   各路取 top-N 合并去重，形成 50~100 候选（含噪声）。
3. **重排序**（第 3 阶段，`app/knowledge/rerank.py`）：
   RRF 融合（只吃名次，解决多路分数不可比）+ 特征线性加权（相似度/命中词数/多路命中），
   对候选集精细打分排序；再按 min_score 过滤不可信结果（纯向量低分且无关键词命中）、
   截断 top_k。默认零外部依赖、确定性可解释；Reranker 为 Protocol，可插 cross-encoder。

SQL 层设计（沿用旧版优点）：
- 向量检索用 `embedding <=> %s::vector`（余弦距离），metadata 过滤在 SQL 层
  JOIN knowledge_documents 完成，让数据库把最相关行压缩到 top-N，添加过滤条件，不全表扫描不回拉全表；
- keywords ILIKE 用命中词数排序，同样 SQL 层完成过滤。


只读执行：复用 ReadOnlyExecutor（agent_reader 角色，仅 SELECT）。

为什么 JOIN documents 而不是用 chunks.metadata：
- department/brand/market 在 documents 上是权威字段（单一事实来源），
  chunks.metadata 只是冗余快照；JOIN 一次同时拿到标题/来源类型，供注入展示。
"""

from __future__ import annotations

import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from typing import Any, Callable, Optional

from app.cache.ttl_cache import get_rag_cache
from app.config.settings import settings
from app.knowledge.embedder import current_embedding_model, embed_query
from app.knowledge.query_optimizer import QueryContext, QueryOptimizer
from app.knowledge.rerank import FeatureFusionReranker, RerankCandidate, VECTOR_ROUTES
from app.memory.embeddings import vector_to_sql
from app.observability.logging import get_logger
from app.tools.sql.executor import ReadOnlyExecutor

logger = get_logger("knowledge_retriever")

# mock 向量下完全无关文本也可能有哈希碰撞噪声分；低于此值视为"向量召回不可信"
DEFAULT_MIN_SCORE = 0.20
DEFAULT_TOP_K = 5

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

    - vector 族命中（vector / rewrite / hyde / mixed 均按向量相似度分档）：
      ≥0.60 high / 0.30~0.60 medium / 0.20~0.30 low；低于 0.20 的向量结果不可信，
      会被 min_score 过滤，不会作为 vector 返回。
    - keyword 命中（仅字面）按命中词数：≥3 high / 2 medium / 1 low。
    """
    if method in ("vector", "rewrite", "hyde", "mixed"):
        if similarity >= 0.60:
            return "high"
        if similarity >= 0.30:
            return "medium"
        return "low"
    # method == "keyword"：字面命中，相似度不反映语义，改看命中词数
    if hit_count is None or hit_count >= 3:
        return "high"
    return "medium" if hit_count == 2 else "low"


class KnowledgeRetriever:
    """企业知识库检索器（查询优化 + 多路召回 + 重排的三级流水线）。

    缓存（OPT-07 扩展，2026-09-29）：
    - 检索缓存键 = query + 全部 metadata 过滤 + top_k + min_score + embedding 模型
      + 流水线开关（改写/HyDE 是否启用）——开关变化必须自动 miss；
      改写文本本身不进键，由 query_optimizer 的 qopt 缓存把改写结果在 TTL 内钉住，
      保证检索缓存命中率（同一 query 一小时只调一次 LLM，改写稳定 → 检索稳定）。
    - TTL = settings.RAG_CACHE_TTL_SECONDS；知识库重灌后调用 invalidate() 全清；
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
        # 第 1 阶段查询优化器 + 第 3 阶段重排器（默认零依赖特征融合，可插拔替换）
        self._query_optimizer = QueryOptimizer()
        self.reranker = FeatureFusionReranker()

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
        """检索缓存键：query + 全部过滤条件 + 参数 + embedding 模型 + 流水线开关。

        改写文本不进键的原因：改写由 LLM 生成，若进键则改写结果微变就整键 miss，
        检索缓存命中率被打爆；开关进键保证"配置变了结果必须重算"。
        """
        if not settings.CACHE_ENABLED:
            return None
        parts = [
            (query or "").strip(),
            department or "", brand or "", market or "", document_type or "",
            str(top_k), str(min_score),
            current_embedding_model(),  # 向量模型变化 → 缓存自动失效
            str(int(settings.RAG_QUERY_REWRITE_ENABLED)),
            str(int(settings.RAG_HYDE_ENABLED)),
        ]
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
        return f"rag:{digest}"

    # ------------------------------------------------------------------
    # 公开入口：三级流水线编排
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
        """混合检索：查询优化 → 多路召回 → 重排序（三级流水线）。

        Args:
            query: 检索问题（用户任务/问题原文）。
            department / brand / market / document_type: metadata 过滤（可选）。
            top_k: 返回条数（默认 self.top_k）。
            min_score: 可信度门槛。纯向量低分（低于此值）且无关键词命中的结果
                会被过滤——保留旧版"不硬塞向量噪声"语义；关键词高命中不受此限。

        Returns:
            [{"chunk_id", "document_id", "chunk_index", "title", "source_type",
              "department", "brand", "market", "content", "similarity", "method",
              "confidence", "routes", "rerank_score"}]
            method ∈ {"vector","rewrite","hyde","keyword","mixed"}；
            routes 列出该 chunk 命中的全部路（可观测）。
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

        start = time.perf_counter()
        # ---- 1. 查询优化：改写 + HyDE（失败自动降级为仅原 query，绝不抛异常） ----
        qctx = self._preprocess_query(query)

        # ---- 2. 多路召回：原 query / 改写 / HyDE 向量 + 关键词 ILIKE（并行） ----
        route_hits = self._multi_recall(
            qctx, department=department, brand=brand, market=market,
            document_type=document_type,
        )

        # ---- 3. 融合 + 重排：RRF + 特征融合打分 → min_score 过滤 → top_k 截断 ----
        result = self._rerank(route_hits, top_k=top_k, min_score=min_score)
        result = [self._stamp_confidence(h) for h in result]

        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "knowledge.retrieve.done",
            query=(query or "")[:60], routes={k: len(v) for k, v in route_hits.items()},
            candidates=sum(len(v) for v in route_hits.values()),
            hits=len(result), duration_ms=duration_ms,
            rewrite=qctx.used_rewrite, hyde=qctx.used_hyde,
        )
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
    # 第 1 阶段：查询优化
    # ------------------------------------------------------------------
    def _preprocess_query(self, query: str) -> QueryContext:
        """查询优化（改写 + HyDE）。任何异常都降级为仅原 query，不打断检索主链路。"""
        try:
            return self._query_optimizer.optimize(query)
        except Exception as exc:
            logger.warning("knowledge.retrieve.preprocess_fail", error=str(exc)[:200])
            return QueryContext(original=(query or "").strip())

    # ------------------------------------------------------------------
    # 第 2 阶段：多路召回
    # ------------------------------------------------------------------
    def _multi_recall(
        self,
        qctx: QueryContext,
        *,
        department: Optional[str],
        brand: Optional[str],
        market: Optional[str],
        document_type: Optional[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """第 2 阶段：多路召回——每个检索视角独立查一遍知识库，结果合并。

        为什么是多次检索：向量检索对"说法"敏感。原 query（用户措辞）、改写
        （业务术语）、HyDE（文档口吻）是三个不同文本，任一视角命中都能救回
        漏召回；关键词路再兜字面专有词（型号/代码/冷门术语）。
        为什么并行：各路是无依赖的独立 IO（embed + SQL），串行会把延迟相加；
        并行后总耗时 ≈ 最慢一路。ReadOnlyExecutor 每次新建连接、缓存带锁，
        线程安全。单路时直接执行，不起线程池（避免无谓开销）。
        qctx.vector_queries()  →  [("vector", 原query), ("rewrite", 改写query), ("hyde", 假想文档)]
                                   ↓                        ↓                        ↓
                             embed_query(原)          embed_query(改写)        embed_query(假想文档)
                                   ↓                        ↓                        ↓
                           _vector_search(SQL#1)      _vector_search(SQL#2)     _vector_search(SQL#3)
                    （各取 top-40，3 路并行）
                                   ↓                        ↓                        ↓
                              └──────────── 合并去重 → 候选集（实测 5~28 条）→ 重排 ────────────┘

        """
        recall_n = settings.RAG_RECALL_N_PER_ROUTE

        # ---- 1. 组装任务：[(路由名, 零参函数)]，每个函数执行时各自独立跑 ----
        tasks: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = []

        # 向量路：每个视角文本各做一次 embed + SQL 检索（top-40/路）
        for route, text in qctx.vector_queries():
            tasks.append(
                (route, partial(
                    self._vector_search_by_text, text, route=route,
                    department=department, brand=brand, market=market,
                    document_type=document_type, top_k=recall_n,
                ))
            )

        # 关键词路：只用原 query 做 ILIKE 字面兜底。改写/HyDE 文本不做关键词——
        # 它们的语义已由各自向量路覆盖，关键词路只需抓住用户原话里的专有词。
        if _extract_keywords(qctx.original):
            tasks.append(
                ("keyword", partial(
                    self._keyword_search, qctx.original,
                    department=department, brand=brand, market=market,
                    document_type=document_type, top_k=recall_n,
                ))
            )

        # ---- 2. 执行：单路直接跑；多路用线程池并行，逐个收集结果 ----
        route_hits: dict[str, list[dict[str, Any]]] = {}
        if len(tasks) == 1:
            name, fn = tasks[0]
            route_hits[name] = fn()
            return route_hits

        with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as pool:
            futures = {pool.submit(fn): name for name, fn in tasks}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    route_hits[name] = future.result()
                except Exception as exc:
                    # 单路失败不拖垮整次检索：该路置空，其余路照常参与融合
                    logger.warning("knowledge.retrieve.route_fail", route=name, error=str(exc)[:200])
                    route_hits[name] = []
        return route_hits

    def _vector_search_by_text(
        self,
        text: str,
        *,
        route: str,
        department: Optional[str],
        brand: Optional[str],
        market: Optional[str],
        document_type: Optional[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """向量路执行体：文本 → 向量化 → SQL 检索（独立方法，参数显式，不依赖闭包）。

        与 _vector_search 的分工：_vector_search 接收**已算好的向量**（复用/测试用），
        本方法负责"文本 → 向量"这一步，是多路召回任务的统一执行入口。
        """
        qvec = embed_query(text)
        return self._vector_search(
            qvec, text, route=route,
            department=department, brand=brand, market=market,
            document_type=document_type, top_k=top_k,
        )

    # ------------------------------------------------------------------
    # 向量检索（单路）
    # ------------------------------------------------------------------
    def _vector_search(
        self,
        qvec: list[float],
        query: str,
        *,
        route: str,
        department: Optional[str],
        brand: Optional[str],
        market: Optional[str],
        document_type: Optional[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """一路向量检索：余弦距离最近 top_k（SQL 层完成 metadata 过滤）。

        route ∈ {"vector","rewrite","hyde"}，标记该路来源，融合阶段据此区分向量分。
        """
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
        # `embedding <=> %s::vector` 算出的是**向量距离**：值越小 = 越相关，范围约 [0,2]；
        # 相似度 = 1 - 距离。
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
        return [self._normalize(r) | {"method": route} for r in result["rows"]]

    # ------------------------------------------------------------------
    # 关键词检索（单路，ILIKE 字面兜底）
    # ------------------------------------------------------------------
    def _keyword_search(
        self,
        query: str,
        *,
        department: Optional[str],
        brand: Optional[str],
        market: Optional[str],
        document_type: Optional[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """关键词路：OR 匹配任一关键词，按命中词数降序取 top_k。

        与向量路并列的一路召回（不再只是"向量分低的兜底"）：
        字面匹配对型号/代码/冷门术语等向量难以区分的场景是强信号。
        """
        keys = _extract_keywords(query)
        if not keys:
            return []
        or_conds = ["c.content ILIKE %s"] * len(keys)
        hit_expr = " + ".join(["(c.content ILIKE %s)::int"] * len(keys))
        hit_params = [f"%{k}%" for k in keys]
        params: list = hit_params + hit_params  # 命中数表达式 + OR 条件各一份，顺序严格对应
        filters: list[str] = []
        if department:
            filters.append("d.department = %s")
            params.append(department)
        if brand:  # 与向量路对齐的过滤能力（旧版漏了 brand，2026-09-29 补齐）
            filters.append("d.brand = %s")
            params.append(brand)
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
    # 第 3 阶段：融合 + 重排序
    # ------------------------------------------------------------------
    def _merge_candidates(
        self, route_hits: dict[str, list[dict[str, Any]]]
    ) -> list[RerankCandidate]:
        """多路 hits → 按 chunk_id 合并去重的候选集（保留每路名次/原始分作为重排证据）。

        合并规则：
        - 同一 chunk 被多路召回：向量分取各路最高（sim_feature），命中词数取关键词路值；
        - 纯关键词命中 chunk（无向量分）：对外 similarity=1.0（与旧版 keyword 语义一致），
          重排特征 sim_feature=0.0（其信号由 hit_count + RRF 承担，不污染相似度归一化）；
        - method = mixed（≥2 路）或单路名，供 confidence 分档。
        """
        merged: dict[str, dict[str, Any]] = {}
        for route, hits in route_hits.items():
            for rank, h in enumerate(hits, start=1):  # 每路内部已排序，名次 = 位置
                base = merged.get(h["chunk_id"])
                if base is None:
                    base = {
                        **{k: h[k] for k in (
                            "chunk_id", "document_id", "chunk_index", "title", "source_type",
                            "department", "brand", "market", "content",
                        )},
                        "sim_feature": 0.0,
                        "hit_count": None,
                        "route_scores": {},
                        "route_ranks": {},
                    }
                    merged[h["chunk_id"]] = base
                base["route_scores"][route] = h["similarity"]
                base["route_ranks"][route] = rank
                if route in VECTOR_ROUTES:
                    base["sim_feature"] = max(base["sim_feature"], h["similarity"])
                base["hit_count"] = max(base["hit_count"] or 0, h.get("hit_count") or 0)

        candidates: list[RerankCandidate] = []
        for chunk_id, base in merged.items():
            routes = list(base["route_ranks"])
            method = "mixed" if len(routes) > 1 else routes[0]
            vector_sim = base["sim_feature"]
            # 对外 similarity：向量路有真实分用真实分；纯关键词命中回退 1.0（旧语义）
            display_sim = vector_sim if vector_sim > 0 else 1.0
            candidates.append(
                RerankCandidate(
                    chunk_id=chunk_id,
                    content=base["content"],
                    route_ranks=base["route_ranks"],
                    route_scores=base["route_scores"],
                    similarity=round(display_sim, 4),
                    sim_feature=round(vector_sim, 4),
                    hit_count=base["hit_count"],
                    method=method,
                    payload={  # 展示字段快照，重排后原样回传组装返回 dict
                        "chunk_id": base["chunk_id"],
                        "document_id": base["document_id"],
                        "chunk_index": base["chunk_index"],
                        "title": base["title"],
                        "source_type": base["source_type"],
                        "department": base["department"],
                        "brand": base["brand"],
                        "market": base["market"],
                        "content": base["content"],
                    },
                )
            )
        return candidates

    def _cap_candidates(self, candidates: list[RerankCandidate]) -> list[RerankCandidate]:
        """候选集保护：超过上限时按"多路命中数 + 相似度"粗排截断。

        特征融合打分本身 O(n)，100 条候选毫秒级；此限制只为防止极端数据量下
        候选爆炸拖慢重排，不是正常路径的瓶颈。
        """
        max_c = settings.RAG_RECALL_MAX_CANDIDATES
        if len(candidates) <= max_c:
            return candidates
        # 粗排分：多路命中数优先（交叉验证信号最强），再按向量相似度
        ranked = sorted(candidates, key=lambda c: (len(c.route_ranks), c.sim_feature), reverse=True)
        logger.info("knowledge.retrieve.cap", candidates=len(candidates), capped=max_c)
        return ranked[:max_c]

    def _rerank(
        self,
        route_hits: dict[str, list[dict[str, Any]]],
        *,
        top_k: int,
        min_score: float,
    ) -> list[dict[str, Any]]:
        """融合 + 重排：合并去重 → RRF+特征融合打分 → min_score 过滤 → top_k 截断。"""
        candidates = self._merge_candidates(route_hits)
        if not candidates:
            return []
        candidates = self._cap_candidates(candidates)

        ranked = self.reranker.rerank(candidates)  # [(candidate, score), ...] 降序
        out: list[dict[str, Any]] = []
        for cand, score in ranked:
            # min_score 语义（保留旧版"不硬塞向量噪声"）：
            # 纯向量低分（相似度低于门槛）且无关键词命中 → 不可信，过滤；
            # 关键词高命中（hit_count>0）不受限——字面证据足够，避免漏检。
            if cand.similarity < min_score and not (cand.hit_count or 0):
                continue
            out.append(
                cand.payload
                | {
                    "similarity": cand.similarity,
                    "method": cand.method,
                    "hit_count": cand.hit_count,
                    "routes": sorted(cand.route_ranks),   # 命中的全部路（可观测）
                    "rerank_score": score,                 # 重排综合分（相对值，仅供排序）
                }
            )
            if len(out) >= top_k:
                break
        return out

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
