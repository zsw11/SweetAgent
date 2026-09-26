"""记忆决策裁判（LLM，设计文档 35 节进阶）。

相似度只负责**召回**候选，是否替代/合并/新增由 LLM 判断语义关系——
解决"阈值两难"（高则去重失效、低则误覆盖）。参考 Mem0 / LangMem / ChatGPT Memory 的
ADD/UPDATE/DELETE/NONE 决策模式。

决策结果：
- unrelated → ADD：与所有候选无关，新增
- duplicate → NONE：同一件事的重复表达，不新增，只刷新旧条 evidence/confidence
- supplement → MERGE：对某个候选的补充/细化，旧条作废（superseded），写入合并后内容
- conflict / negation → UPDATE 或 NONE：矛盾/否定，用户最新表达且置信度足够则版本化更新，否则保留旧条

LLM 不可用或输出解析失败时返回 None，由调用方走降级规则（简单阈值版本化）。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from app.llm import get_chat_model, llm_available
from app.llm.structured import extract_json, invoke_structured, invoke_text
from app.observability.logging import get_logger

logger = get_logger("memory_judge")


# ---------------------------------------------------------------------------
# OPT-04 结构化输出：记忆裁判 schema（with_structured_output 用）
# ---------------------------------------------------------------------------

class JudgeOutput(BaseModel):
    """裁判输出 schema（与 JUDGE_PROMPT 要求 JSON 同构）。"""
    relation: str = "unrelated"
    target_id: Optional[Any] = None
    event: str = "ADD"
    new_content: str = ""
    reason: str = ""

JUDGE_PROMPT = """你是用户长期记忆的"冲突裁判"。判断新记忆与候选旧记忆的关系，决定如何写入记忆库。

新记忆（type={new_type}，confidence={new_conf}）：
{new}

候选旧记忆（id / type / content / confidence）：
{candidates}

关系与事件规则：
- unrelated：新记忆与所有候选都无关 → event=ADD（直接新增）
- duplicate：新记忆与某候选是同一件事的重复表达（只改写字句、换说法）→ event=NONE（不新增，仅刷新该候选的 evidence/confidence）
- supplement：新记忆是对某候选的补充/细化（信息更全）→ event=MERGE（旧候选作废，写入合并后的完整内容）
- conflict：新记忆与某候选矛盾（如偏好变更、说法相反）→ event=UPDATE（新记忆置信度不低于旧候选时，旧候选作废、写入新记忆）或 NONE（无法确定新旧时保留旧候选）
- negation：新记忆否定某候选（"喜欢X" vs "不喜欢X"）→ event=UPDATE（新记忆置信度显著更高）或 NONE（否则并存，不冒险覆盖）

只输出 JSON（不要 markdown 代码块）：
{{"relation": "unrelated|duplicate|supplement|conflict|negation", "target_id": 候选id或null, "event": "ADD|NONE|UPDATE|MERGE", "new_content": "MERGE 时填合并后内容，其他情况填空字符串", "reason": "一句话理由"}}

严格一致性约束（违反即输出错误）：
1. relation 与 event 必须一一对应：relation=duplicate ↔ event=NONE；relation=supplement ↔ event=MERGE；relation=conflict/negation ↔ event=UPDATE；relation=unrelated ↔ event=ADD；
2. 区分 duplicate 与 supplement：内容完全相同、只改写字句/换说法 = duplicate（event=NONE，不新增）；新内容确实比候选多了信息（范围扩大、部门归属、新增事实点）= supplement（event=MERGE）。判断不准时按 duplicate/NONE（不新增），不冒险版本化；
3. target_id 必须逐字来自候选列表中真实存在的 id（方括号里的数字），禁止引用不存在的 id；拿不准 target_id 时填 null；
4. reason 文字必须与 relation/event 一致：若 reason 描述为"重复/同一事实/换说法/仅措辞差异"，relation 必须=duplicate、event 必须=NONE，不得在 reason 说重复的同时 event 给 UPDATE/MERGE。

约束：target_id 必须来自候选旧记忆的 id；拿不准时宁选 ADD/NONE，不冒险覆盖。"""


def judge_memory(
    new_content: str,
    new_type: str,
    candidates: list[dict[str, Any]],
    new_confidence: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """调用 LLM 判断新记忆与候选的关系。

    candidates: [{"id": int, "memory_type": str, "content": str, "confidence": float|None}]
    返回 {"relation", "target_id", "event", "new_content", "reason"}；
    LLM 不可用 / 解析失败 / 无候选 → None。
    """
    if not candidates:
        return None
    if not llm_available():
        return None

    cand_lines = [
        f"[{c['id']}] type={c.get('memory_type') or '?'}, confidence={c.get('confidence')}, content={c['content'][:200]}"
        for c in candidates
    ]
    model = get_chat_model(tier="small")
    from langchain_core.messages import HumanMessage, SystemMessage

    prompt = JUDGE_PROMPT.format(
        new=new_content[:1000],
        new_type=new_type or "?",
        new_conf="None" if new_confidence is None else round(new_confidence, 2),
        candidates="\n".join(cand_lines)[:3000],
    )
    messages = [
        SystemMessage(content="你是记忆冲突裁判，严格输出 JSON。"),
        HumanMessage(content=prompt),
    ]
    # OPT-04：结构化通道优先，失败降级文本解析
    d = invoke_structured(model, JudgeOutput, messages, logger_name="memory_judge")
    if d is not None:
        return _normalize_judge(d)
    text = invoke_text(model, messages, logger_name="memory_judge")
    if text is None:
        logger.warning("memory.judge.failed")
        return None
    return _parse_judge_json(text)


def _normalize_judge(obj: dict[str, Any]) -> dict[str, Any]:
    """把裁判 dict 规范化到约定白名单（结构化通道与文本降级共用）。"""
    event = str(obj.get("event") or "ADD").upper()
    if event not in ("ADD", "NONE", "UPDATE", "MERGE"):
        event = "ADD"
    return {
        "relation": str(obj.get("relation") or "unrelated").lower(),
        "target_id": obj.get("target_id"),
        "event": event,
        "new_content": str(obj.get("new_content") or ""),
        "reason": str(obj.get("reason") or ""),
    }


def _parse_judge_json(raw: str) -> Optional[dict[str, Any]]:
    """从 LLM 输出中提取裁判 JSON（统一委托 extract_json + normalize）。"""
    obj = extract_json(raw)
    if obj is None:
        return None
    return _normalize_judge(obj)
