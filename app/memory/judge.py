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

import json
from typing import Any, Optional

from app.llm import get_chat_model, llm_available
from app.observability.logging import get_logger

logger = get_logger("memory_judge")

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
    try:
        resp = model.invoke([
            SystemMessage(content="你是记忆冲突裁判，严格输出 JSON。"),
            HumanMessage(content=prompt),
        ])
        return _parse_judge_json(str(resp.content))
    except Exception as exc:  # LLM 调用失败不阻断写入，走降级
        logger.warning("memory.judge.failed", error=str(exc))
        return None


def _parse_judge_json(raw: str) -> Optional[dict[str, Any]]:
    t = (raw or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:].strip()
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None
    if not isinstance(obj, dict):
        return None
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
