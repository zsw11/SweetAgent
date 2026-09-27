"""输出敏感字段脱敏（OPT-06 B）。

目标：即使注入被绕过 / LLM 输出了敏感信息，回答里也不出现明文 PII。
覆盖 3 类：
- 手机号：1[3-9] 开头 11 位 → 138****1234（保留前 3 后 4）；
- 邮箱：user@domain → u***@domain（保留首字符 + 域名）；
- 银行卡 / 长数字串：16~19 位连续数字 → 前 4 **** 后 4。

设计取舍：
- **不脱敏业务金额**（GMV / 毛利 / 退款额是分析对象，全掩码会让业务回答失效）；
  手机号/长数字模式已用边界断言 (?<!\\d)(?!\\d) 隔离，正常金额（如 1234567.89）
  不会命中；11 位整数（如订单号）若以 1[3-9] 开头会被误掩——这是 PII 优先的取舍；
- **递归 mask_object**：decision_result 是嵌套 dict/list，任何字符串字段都过一遍，
  覆盖 summary / findings / recommendations 等所有文本出口；
- **只改回答，不改数据源**：DB 原文与记忆库不变，仅"返回给用户"的层脱敏。
"""

from __future__ import annotations

import re
from typing import Any

# (类型, 正则, 掩码函数) —— 顺序重要：长数字串先于手机号（16~19 位不吃 11 位），
# 邮箱最后（@ 分隔结构独立，不会与前两者冲突）。
_MASK_PATTERNS: list[tuple[str, str, Any]] = [
    (
        "card",
        r"(?<!\d)\d{16,19}(?!\d)",
        lambda m: f"{m.group()[:4]}****{m.group()[-4:]}",
    ),
    (
        "phone",
        r"(?<!\d)1[3-9]\d{9}(?!\d)",
        lambda m: f"{m.group()[:3]}****{m.group()[-4:]}",
    ),
    (
        "email",
        r"[\w.+-]+@[\w-]+(\.[\w-]+)+",
        lambda m: f"{m.group()[:1]}***@{m.group().split('@', 1)[1]}",
    ),
]


def mask_text(text: str) -> tuple[str, list[dict[str, int]]]:
    """对单段文本脱敏，返回 (掩码后文本, applied=[{"type", "count"}] 去重)。

    applied 供日志 `masking.applied` 观测（哪些类型掩了几处）。
    """
    if not text:
        return text, []
    masked = text
    applied: list[dict[str, int]] = []
    for mtype, pattern, repl in _MASK_PATTERNS:
        found = re.findall(pattern, masked)
        if found:
            masked = re.sub(pattern, repl, masked)
            applied.append({"type": mtype, "count": len(found)})
    return masked, applied


def mask_object(obj: Any) -> tuple[Any, list[dict[str, int]]]:
    """递归脱敏 dict / list / str（嵌套任意深度）；其他类型原样返回。

    Returns:
        (脱敏后的对象, 全部 applied 合并列表)
    """
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        applied_all: list[dict[str, int]] = []
        for k, v in obj.items():
            out[k], applied = mask_object(v)
            applied_all.extend(applied)
        return out, applied_all
    if isinstance(obj, list):
        out_list: list[Any] = []
        applied_all = []
        for item in obj:
            masked_item, applied = mask_object(item)
            out_list.append(masked_item)
            applied_all.extend(applied)
        return out_list, applied_all
    return obj, []
