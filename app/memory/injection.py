"""长期记忆分层注入（设计文档 34-35 节）。

分层原则（讨论确认）：
- Manager（规划）：只注入用户级信息——user_profiles + user_preferences 全量 + user_memories 无部门标签的通用分级注入（强约束全量 + 弱记忆 top-k）
- 部门子 Agent（执行）：注入本部门上下文——business_preferences（scope=本部门∪global）+ user_memories（department=本部门∪无标签）分级注入
  （知识库 knowledge_chunks 检索由部门 Agent 内部已有逻辑负责；跨部门结论由 main_graph 的 context_builder 注入）

注入产物统一为 dict，经 BaseDepartmentAgent._context_text() 序列化进 prompt 的 {context} 占位。

分级规则（考点四十五）：memory_type ∈ {preference, rule} 是强约束（答错代价高）→ 全量注入；
memory_type ∈ {fact, conclusion} 是弱记忆（少个佐证）→ 按相似度 top-k；
token 预算 MEMORY_INJECT_TOKEN_BUDGET 内"保强弃弱"（_fit_budget）。
"""

from __future__ import annotations

from typing import Any, Optional

from app.config.settings import settings
from app.memory.conversation import count_tokens
from app.memory.profile import (
    get_business_preferences,
    get_preferences,
    get_profiles,
)
from app.memory.semantic import search_memories
from app.observability.logging import get_logger

logger = get_logger("memory_injection")

# 已知部门（用于粗筛）
KNOWN_DEPARTMENTS = ("operation", "finance", "logistics", "product")

# memory_type → 注入标注（区分约束强度：preference/rule 强，fact/conclusion 弱）
_TYPE_LABEL = {
    "preference": "偏好",
    "fact": "事实",
    "conclusion": "历史结论",
    "rule": "规则",
}

# 记忆分级：强约束（答错代价高，全量注入）vs 弱记忆（少个佐证，按相似度 top-k）
_STRONG_TYPES = ("preference", "rule")
_WEAK_TYPES = ("fact", "conclusion")
_STRONG_CAP = 100  # 强约束单类型检索上限（实际量小，兜底防极端）


def _annotate(m: dict) -> str:
    """记忆注入文本：带类型标注（如（偏好）用户要求用美元结算）。"""
    label = _TYPE_LABEL.get(m.get("memory_type") or "", "")
    return f"（{label}）{m['content']}" if label else m["content"]


def _search_strong(user_id: str, query: str, departments: Optional[list[str]]) -> list[dict]:
    """强约束（preference/rule）全量检索：不受相似度 top-k 限制，按 id 合并去重。"""
    merged: dict[int, dict] = {}
    for t in _STRONG_TYPES:
        for m in search_memories(user_id, query, departments=departments, top_k=_STRONG_CAP, memory_type=t):
            merged.setdefault(m["id"], m)
    return sorted(merged.values(), key=lambda m: m["similarity"], reverse=True)


def _search_weak(user_id: str, query: str, departments: Optional[list[str]], top_k: int) -> list[dict]:
    """弱记忆（fact/conclusion）按相似度 top-k 检索（排除强类型）。"""
    return search_memories(
        user_id, query, departments=departments, top_k=top_k,
        exclude_types=list(_STRONG_TYPES),
    )


def _fit_budget(strong: list[dict], weak: list[dict], budget: int) -> tuple[list[dict], int, int]:
    """token 预算内"保强弃弱"：强约束在前、弱记忆按相似度在后，超预算截断。

    Returns: (注入列表, 被丢弃的弱记忆条数, 实际占用 token)
    """
    ordered = list(strong) + list(weak)
    used = 0
    kept: list[dict] = []
    dropped = 0
    for m in ordered:
        cost = count_tokens(_annotate(m))
        if used + cost > budget:
            if m["memory_type"] in _WEAK_TYPES:
                dropped += 1
            continue  # 强约束超预算兜底：按相似度顺序截断（极端情况）
        kept.append(m)
        used += cost
    return kept, dropped, used

# 轻量意图部门预判关键词（Manager 规划前粗筛用，零成本；不精确，只求淘汰明显无关）
_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "operation": ("销量", "订单", "gmv", "转化", "广告", "流量", "促销", "店铺", "评论", "运营", "渠道", "sku"),
    "finance": ("利润", "收入", "成本", "毛利", "广告费", "汇率", "退款", "贡献利润", "亏损", "财务", "费用", "roi"),
    "logistics": ("库存", "在途", "仓储", "运输", "时效", "缺货", "承运", "物流", "配送", "周转", "安全库存"),
    "product": ("产品", "开发", "竞品", "趋势", "行业", "消费者", "生命周期", "新品", "设计", "规格", "评分"),
}


def guess_intent_departments(question: str) -> list[str]:
    """轻量预判问题意图部门（关键词命中；不命中返回空 = 跳过粗筛）。"""
    q = (question or "").lower()
    hit = [name for name, kws in _INTENT_KEYWORDS.items() if any(k in q for k in kws)]
    return hit


# ---------------------------------------------------------------------------
# Manager 级注入
# ---------------------------------------------------------------------------

def build_manager_memory(user_id: str, user_question: str) -> dict[str, Any]:
    """Manager 规划前注入的用户记忆（用户级信息 + 分级非结构化记忆）。"""
    profiles = get_profiles(user_id)
    preferences = get_preferences(user_id)
    # 通用记忆：无部门标签（department 为空）——注入侧过滤带标签的
    strong = [m for m in _search_strong(user_id, user_question, None) if not m["department"]]
    weak = [m for m in _search_weak(user_id, user_question, None, settings.MEMORY_RECALL_TOP_K) if not m["department"]]
    memories, dropped, used = _fit_budget(strong, weak, settings.MEMORY_INJECT_TOKEN_BUDGET)

    memory = {
        "profiles": profiles,
        "preferences": preferences,
        "memories": [_annotate(m) for m in memories],
    }
    if not any(memory.values()):
        return {}
    logger.info(
        "memory.inject.manager",
        user_id=user_id,
        profiles=len(profiles),
        preferences=len(preferences),
        strong=len(strong),
        weak=len(weak),
        injected=len(memories),
        dropped_weak=dropped,
        tokens=used,
    )
    return memory


# ---------------------------------------------------------------------------
# 部门级注入
# ---------------------------------------------------------------------------

def build_department_memory(user_id: str, agent_name: str, user_question: str) -> dict[str, Any]:
    """部门子 Agent 执行前注入的本部门上下文。

    返回 dict（可直接并入 context_builder 的 context）：
      business_rules: business_preferences（scope=本部门 ∪ global）
      department_memories: user_memories（department=本部门 ∪ 无标签）分级注入
    """
    if agent_name not in KNOWN_DEPARTMENTS:
        return {}
    rules = get_business_preferences(scope=agent_name)
    strong = _search_strong(user_id, user_question, [agent_name])
    weak = _search_weak(user_id, user_question, [agent_name], settings.MEMORY_RECALL_TOP_K)
    memories, dropped, used = _fit_budget(strong, weak, settings.MEMORY_INJECT_TOKEN_BUDGET)
    ctx: dict[str, Any] = {}
    if rules:
        ctx["business_rules"] = rules
    if memories:
        ctx["department_memories"] = [_annotate(m) for m in memories]
    if ctx:
        logger.info(
            "memory.inject.department",
            user_id=user_id,
            agent=agent_name,
            rules=len(rules),
            strong=len(strong),
            weak=len(weak),
            injected=len(memories),
            dropped_weak=dropped,
            tokens=used,
        )
    return ctx
