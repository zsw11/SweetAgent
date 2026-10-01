"""重排序：对多路召回候选集做精细打分与筛选（RAG 三级流水线第 3 阶段）。

为什么召回之后还要重排：
- 多路召回（向量 ×N + 关键词）各路的分数尺度不可比——余弦相似度 0~1 vs 关键词命中
  词数 0~N，直接按某一路的分数排序，会把其他路的高质量结果挤到后面；
- 候选集 50~100 条必然含噪声（向量召回对无关文本也有碰撞噪声分），需要一个
  "综合打分 + 排序"环节，把最相关的文档排到最前、低分噪声沉底。

默认实现 FeatureFusionReranker（零外部依赖，确定性、可解释、毫秒级）：
1. RRF（Reciprocal Rank Fusion）：score = Σ 1/(k + rank_i)，只依赖"名次"不依赖"原始分"，
   天然解决多路分数不可比——这是 Elasticsearch 混合检索的标准融合法；
2. 特征融合：rrf 分 × w_rrf + 归一化相似度 × w_sim + 命中词数归一 × w_hit
   + 多路命中 bonus（同一 chunk 被多路召回 = 更可信）；
3. 排序后由检索器截断取 top_k。

可插拔设计：Reranker 是 Protocol。将来想上 cross-encoder（如 bge-reranker，装包 +
下模型即可）时，新写一个类实现同一接口，检索器编排层零改动。

关于权重为什么这么设（0.5 / 0.3 / 0.2）：
- RRF 分既跨路可比又对"多路都靠前"敏感，是混合检索最稳的信号，权重最大；
- 相似度是单路内的连续信号，能区分同路内的细微差异，次之；
- 命中词数是字面信号，对向量失败场景兜底，但不能喧宾夺主（否则退化成纯关键词检索）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("knowledge_rerank")

# RRF 平滑常数（经典取值 60）：越小 = 名次差的影响越大，越大 = 越扁平
RRF_K = 60

# 向量族路由名（与 query_optimizer 的 vector_queries() 路由名保持一致）
VECTOR_ROUTES = frozenset({"vector", "rewrite", "hyde"})


@dataclass
class RerankCandidate:
    """重排输入：一个候选 chunk + 各路召回证据。

    - route_ranks:  {route: 该路内的名次(1-based)}——RRF 只吃名次，不吃原始分；
    - route_scores: {route: 该路原始分}——保留可观测（日志 / 排障用）；
    - similarity:   对外展示的相似度：向量路最高分；纯关键词命中时为 1.0
                    （保持与旧版 keyword 命中相似度语义一致，供下游 confidence 分档）；
    - sim_feature:  重排特征用相似度：**仅真实向量分**，纯关键词命中为 0.0
                    （keyword-only chunk 的信号全靠 hit_count + RRF，不占归一化尺度）；
    - hit_count:    关键词命中词数（无关键词命中为 None）；
    - payload:      展示字段快照（title/source_type/department/brand/market/content 等），
                    重排后由检索器原样回传组装返回 dict——重排器不关心 payload 内容，
                    只把它当"携带的行李"，这样 rerank 模块不依赖知识库表结构。
    """

    chunk_id: str
    content: str
    route_ranks: dict[str, int]
    route_scores: dict[str, float]
    similarity: float
    sim_feature: float
    hit_count: Optional[int] = None
    method: str = "vector"
    payload: dict[str, Any] = field(default_factory=dict)


class Reranker(Protocol):
    """重排器协议：候选 → 按相关度降序的 [(candidate, rerank_score), ...]。"""

    def rerank(self, candidates: list[RerankCandidate]) -> list[tuple[RerankCandidate, float]]:
        ...


class FeatureFusionReranker:
    """零依赖重排：RRF 融合 + 特征线性加权（默认实现，确定性可解释）。

    Args 默认读 settings，显式传参可覆盖（测试 / 调参用）。
    """

    def __init__(
        self,
        *,
        w_rrf: Optional[float] = None,
        w_sim: Optional[float] = None,
        w_hit: Optional[float] = None,
        multi_route_bonus: Optional[float] = None,
    ):
        self.w_rrf = settings.RAG_RERANK_W_RRF if w_rrf is None else w_rrf
        self.w_sim = settings.RAG_RERANK_W_SIM if w_sim is None else w_sim
        self.w_hit = settings.RAG_RERANK_W_HIT if w_hit is None else w_hit
        self.multi_route_bonus = (
            settings.RAG_RERANK_MULTI_ROUTE_BONUS if multi_route_bonus is None else multi_route_bonus
        )

    def rerank(self, candidates: list[RerankCandidate]) -> list[tuple[RerankCandidate, float]]:
        """计算每个候选的综合相关度分，按降序返回 (candidate, score)。

        分数是相对值（只用于排序），不做绝对阈值判定；阈值过滤由检索器按
        min_score / top_k 负责。
        """
        if not candidates:
            return []

        # 相似度 min-max 归一化：跨候选缩放，消除绝对尺度差异（相对排序不需要绝对值）
        sims = [c.sim_feature for c in candidates]
        sim_min, sim_max = min(sims), max(sims)
        sim_range = (sim_max - sim_min) or 1.0  # 全部同分时避免除零

        # 命中词数归一：hit_count / 最大 hit_count（0~1），没有关键词命中的候选为 0
        max_hit = max((c.hit_count or 0) for c in candidates) or 1

        scored: list[tuple[RerankCandidate, float]] = []
        for c in candidates:
            # RRF：把"该 chunk 在每路的名次"融合成一个跨路可比的分；
            # 多路都靠前的 chunk 自然得分高（1/(60+1) + 1/(60+2) > 单路 1/(60+1)）。
            rrf = sum(1.0 / (RRF_K + rank) for rank in c.route_ranks.values())

            sim_norm = (c.sim_feature - sim_min) / sim_range
            hit_norm = min((c.hit_count or 0) / max_hit, 1.0)

            # 多路命中加分：每多一路 +bonus（含关键词路）——多路召回都认为它相关，
            # 比单路高分更可信（信号交叉验证）。
            extra_routes = max(0, len(c.route_ranks) - 1)
            score = (
                self.w_rrf * rrf
                + self.w_sim * sim_norm
                + self.w_hit * hit_norm
                + self.multi_route_bonus * extra_routes
            )
            scored.append((c, round(score, 6)))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored


def route_summary(candidates: list[RerankCandidate]) -> dict[str, Any]:
    """候选集的各路召回统计（日志 / 可观测用）：{route: 命中条数}。"""
    summary: dict[str, int] = {}
    for c in candidates:
        for route in c.route_ranks:
            summary[route] = summary.get(route, 0) + 1
    return summary
