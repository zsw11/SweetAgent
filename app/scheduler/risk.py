"""风险分级（NL2Cron，开发日志考点六十四）。

规则（确定性，不依赖 LLM）：
- 能力域基础风险：registry 注册时定义（monitor/knowledge_sync=low，analysis/eval_regression=mid）
- 参数提级：自由文本参数（如 analysis.question 长文本）保持基础级；
  若参数含外部 URL/疑似命令注入特征 → 提级 high（防御性，宁可高判）
- 输出：low/mid/high 三档，存 scheduler_jobs.risk_level，供审核流排序/决策
"""

from __future__ import annotations

import re
from typing import Any

# 疑似注入特征（防御性粗筛：命中即提级 high，交由人工审核）
_INJECTION_PATTERNS = [
    re.compile(r"(https?://|ftp://)", re.I),       # 外部 URL（知识同步域可豁免，见下）
    re.compile(r"(;\s*(drop|delete|update|insert|alter|truncate)\b)", re.I),  # SQL 注入特征
    re.compile(r"(__import__|os\.system|subprocess|eval\s*\()", re.I),        # Python 注入特征
]

# 豁免：知识同步域的 source_dir 允许本地路径（不含 URL/注入则放行）
_ALLOWED_DIR_CHARS = re.compile(r"^[\w\-\/\.\\:]*$")


def evaluate_risk(domain_key: str, params: dict[str, Any]) -> str:
    """按能力域 + 参数计算风险等级（low/mid/high）。"""
    from app.scheduler.registry import get_domain

    domain = get_domain(domain_key)
    base = domain.risk_level if domain else "high"  # 白名单外直接 high
    if base == "high":
        return "high"

    # 参数级提级
    text = ""
    for v in params.values():
        if isinstance(v, str):
            text += v + " "
    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            # 知识同步域 source_dir 可能是 http(s) 源——允许但保持 mid 以下不额外提级
            if domain_key == "knowledge_sync" and pat.pattern.startswith("(https?://"):
                continue
            return "high"
    return base
