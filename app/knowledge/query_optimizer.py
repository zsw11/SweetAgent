"""查询优化器：检索前对用户 query 做改写 + HyDE，产出多视角检索输入（RAG 三级流水线第 1 阶段）。

为什么需要查询优化（多路召回的前提）：
- 用户 query 常是口语 / 含指代 / 缺实体（如"它的红线是多少""这个怎么算"），直接向量化
  召回质量差；改写得到"更适合检索的独立查询"，与原 query **并行**走向量召回，多一个视角。
- HyDE（Hypothetical Document Embeddings）：让 LLM 先"写一段知识库里可能存在的答案文档"，
  再对该假想文档向量化做一路召回。假想文档比问题句更贴近文档的陈述式表述，
  能弥补"问题与文档措辞不对齐"的语义鸿沟——是 query 改写之外最常用的零训练检索增强。

降级策略（本项目无 LLM key 也能全链路跑通，保持 mock 可复现风格）：
- llm_available() 为 False 或调用失败 → rewritten / hyde_doc 置 None，检索器自动跳过对应路，
  等价旧的"仅原 query 向量 + 关键词"行为；
- 改写 / HyDE 结果按 query 短 TTL 缓存（qopt: 前缀，与检索结果缓存 rag: 前缀分离）：
  同一 query 在 TTL 内只调一次 LLM → 改写文本稳定 → 检索结果缓存命中率不受影响。

缓存设计要点（为什么 qopt 与检索缓存分离）：
- 检索缓存键只含"是否启用改写/HyDE"开关，不含改写文本——改写文本不稳定会打爆检索缓存；
- qopt 缓存负责把改写文本在 TTL 内钉住，两层配合：LLM 少调 + 检索结果稳定命中。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from app.cache.ttl_cache import get_rag_cache
from app.config.settings import settings
from app.knowledge.embedder import current_embedding_model
from app.llm import get_chat_model, llm_available
from app.observability.logging import get_logger

logger = get_logger("knowledge_query_optimizer")

# 改写 / HyDE 结果缓存 key 前缀（与检索结果缓存 rag: 前缀分离，互不干扰）
_QOPT_CACHE_PREFIX = "qopt:"

# HyDE 假想文档长度上限（短文即可，过长浪费 token 且引入噪声）
_HYDE_MAX_CHARS = 200

# 查询改写提示词：输出必须是一条"自包含、适合向量检索"的查询，禁止解释。
_REWRITE_PROMPT = """你是企业知识库的检索查询优化器。把下面的用户问题改写成一条适合向量检索的独立查询：
1. 补全指代（"它/这个/那边" → 具体实体）；
2. 明确关键实体与业务术语（部门、品类、市场、指标名等），保持原意；
3. 去掉口语和冗余，输出一句完整、自包含的查询。
只输出改写后的查询本身，不要解释、不要加引号；如果原问题已经适合检索，原样输出。

用户问题：{query}
改写后查询："""

# HyDE 提示词：目标是生成"陈述式答案段落"，而非回答问题本身。
_HYDE_PROMPT = """你是跨境电商行业的资深专家。知识库里可能有这样一篇文档：它用陈述语气回答了下面的问题。
请用陈述语气写一段该文档中可能出现的内容（150 字以内，直接给正文，不要标题、不要解释）。

问题：{query}
文档内容："""


@dataclass
class QueryContext:
    """一次检索的多视角 query 上下文（第 1 阶段产物，喂给第 2 阶段多路召回）。

    - original:    用户原始 query（必走一路向量召回，也用于关键词路）；
    - rewritten:   LLM 改写后的独立查询（None = 改写不可用，跳过该路）；
    - hyde_doc:    假想答案文档（None = HyDE 不可用，跳过该路）；
    - used_rewrite / used_hyde: 可观测标记（False = 因无 LLM / 失败而降级）。
    """

    original: str
    rewritten: Optional[str] = None
    hyde_doc: Optional[str] = None
    used_rewrite: bool = False
    used_hyde: bool = False

    def vector_queries(self) -> list[tuple[str, str]]:
        """返回需要向量化的 (路由名, 文本) 列表，供多路召回逐路 embed。

        路由名同时充当该路的 method 标识：vector / rewrite / hyde。
        """
        pairs: list[tuple[str, str]] = [("vector", self.original)]
        if self.rewritten:
            pairs.append(("rewrite", self.rewritten))
        if self.hyde_doc:
            pairs.append(("hyde", self.hyde_doc))
        return pairs


def _qopt_cache_key(query: str, model: str) -> str:
    """改写结果缓存键：query 文本 + embedding 模型（模型切换自动 miss）。"""
    digest = hashlib.sha256((query or "").strip().encode("utf-8")).hexdigest()
    return f"{_QOPT_CACHE_PREFIX}{model}:{digest}"


class QueryOptimizer:
    """查询优化器（RAG 三级流水线第 1 阶段）。

    只负责"产出多视角 query"，不做任何检索；检索器拿到 QueryContext 后自行决定
    走哪些路。构造参数默认读 settings，显式传参可覆盖（测试 / 按调用方开关）。
    """

    def __init__(
        self,
        *,
        rewrite_enabled: Optional[bool] = None,
        hyde_enabled: Optional[bool] = None,
    ):
        self._rewrite_enabled = (
            settings.RAG_QUERY_REWRITE_ENABLED if rewrite_enabled is None else rewrite_enabled
        )
        self._hyde_enabled = settings.RAG_HYDE_ENABLED if hyde_enabled is None else hyde_enabled

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------
    def optimize(self, query: str) -> QueryContext:
        """对 query 做改写 + HyDE，返回多视角 QueryContext（任何失败都降级为原始 query）。

        降级是"结构性保证"而非"异常处理"：改写 / HyDE 对检索是增益项，
        缺了它们流水线照常跑，只是少一两路召回视角。
        """
        query = (query or "").strip()
        if not query:
            return QueryContext(original="")

        # 结果缓存：同一 query 在 TTL 内只调一次 LLM（改写文本稳定 → 检索缓存命中率高）
        cache_key = _qopt_cache_key(query, current_embedding_model())
        if settings.CACHE_ENABLED:
            cached = get_rag_cache().get(cache_key)
            if cached is not None:
                return cached

        ctx = QueryContext(original=query)
        llm = self._get_llm()
        if llm is not None and self._rewrite_enabled:
            rewritten = self._call_llm(llm, _REWRITE_PROMPT.format(query=query), "rewrite")
            # 空结果 / 改写等于原文 → 无增益，置 None 让检索器只走原 query 路
            if rewritten and rewritten.strip() != query:
                ctx.rewritten = rewritten.strip()
                ctx.used_rewrite = True
        if llm is not None and self._hyde_enabled:
            hyde = self._call_llm(llm, _HYDE_PROMPT.format(query=query), "hyde")
            if hyde:
                ctx.hyde_doc = hyde.strip()[:_HYDE_MAX_CHARS]
                ctx.used_hyde = True

        if settings.CACHE_ENABLED:
            get_rag_cache().set(cache_key, ctx, settings.RAG_CACHE_TTL_SECONDS)
        return ctx

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    @staticmethod
    def _get_llm():
        """LLM 可用才返回模型实例；无 key / 构造失败返回 None（不抛异常打断主链路）。"""
        if not llm_available():
            return None
        try:
            return get_chat_model(tier="small", temperature=0.0)
        except Exception as exc:
            logger.warning("knowledge.qopt.llm_unavailable", error=str(exc)[:200])
            return None

    @staticmethod
    def _call_llm(llm, prompt: str, stage: str) -> Optional[str]:
        """单次 LLM 调用；任何失败（超时 / 限流 / 解析）都降级返回 None，不影响检索主链路。"""
        try:
            resp = llm.invoke(prompt)
            text = (getattr(resp, "content", None) or "").strip()
            return text or None
        except Exception as exc:
            logger.warning("knowledge.qopt.llm_fail", stage=stage, error=str(exc)[:200])
            return None
