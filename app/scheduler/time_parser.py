"""自然语言时间 → cron 解析器（纯函数，可单测，开发日志考点六十四）。

设计要点：
- LLM 不直接吐 cron（时区/语法易错），先吐自然语言 time_expr，由本解析器确定性转 cron
- 支持：每天X点 / 每周X / 每月X日 / 每N小时 / 每N分钟（含频率上限校验）
- 频率上限：最小触发间隔（如 15 分钟），防"每秒钟跑一次"滥用——规则层拦截，不信任 LLM
- 解析失败返回 None（调用方拒绝 + 提示）
"""

from __future__ import annotations

import re
from typing import Optional

from app.config.settings import settings

# 中文星期映射
_WEEKDAY_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_WEEKDAY_EN = {
    "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
    "friday": 5, "saturday": 6, "sunday": 7,
    "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7,
}

# 常用表达模式（时段词 → 12 小时制换算见 _normalize_hour）
_PERIODS = r"(凌晨|早上|上午|中午|下午|晚上|夜里|早|晚)?"
_RE_DAILY_HOUR = re.compile(r"每天?" + _PERIODS + r"(\d{1,2})[点时]")                # 每天下午5点
_RE_WEEKLY = re.compile(r"每周([一二三四五六日天])" + _PERIODS + r"(\d{1,2})[点时]")  # 每周一晚上8点
_RE_MONTHLY = re.compile(r"每月(\d{1,2})(?:号|日)" + _PERIODS + r"(\d{1,2})[点时]")  # 每月1号上午9点
_RE_EVERY_HOUR = re.compile(r"每(\d{1,2})小时")                                      # 每2小时
_RE_EVERY_MIN = re.compile(r"每(\d{1,2})分钟")                                       # 每30分钟
_RE_DAILY = re.compile(r"每天")                                                      # 每天（默认9点）
_RE_HOURLY = re.compile(r"每小时|每个小时")                                           # 每小时


def _normalize_hour(hour: int, period: str) -> Optional[int]:
    """12 小时制 + 时段词 → 24 小时制；非法返回 None。

    规则：凌晨/早上/上午 保持；中午 12→12（11点不存在）；下午/晚上/夜里 hour<12 → +12。
    """
    if not 0 <= hour <= 23:
        return None
    if not period:
        return hour
    if period in ("凌晨", "早上", "上午", "早"):
        return hour if hour <= 11 else None
    if period == "中午":
        return 12 if hour == 12 else hour
    if period in ("下午", "晚上", "夜里", "晚"):
        return hour + 12 if hour < 12 else hour  # 下午13点=13点(24h 直接)
    return hour


def _min_interval_ok(cron_expr: str) -> bool:
    """频率上限校验：cron 最小触发间隔不得小于 settings.SCHEDULER_MIN_INTERVAL_MINUTES。

    解析 cron 为"每分钟/每N分钟/每N小时/每日固定时刻"，粗算最小间隔（分钟）。
    无法判定（如复杂 cron）按通过处理，创建时由服务层再校验。
    """
    parts = cron_expr.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    if minute.startswith("*/"):
        n = int(minute[2:])
        return n >= settings.SCHEDULER_MIN_INTERVAL_MINUTES
    if hour.startswith("*/"):
        n = int(hour[2:])
        return n * 60 >= settings.SCHEDULER_MIN_INTERVAL_MINUTES
    if minute == "*" and hour == "*":
        return 60 >= settings.SCHEDULER_MIN_INTERVAL_MINUTES
    # 固定时刻（如 0 9 * * *）：一天一次，天然满足
    return True


def parse_time_expr(expr: str) -> Optional[str]:
    """把自然语言时间表达解析成标准 cron（5 段）。

    支持：每天9点 / 每天早8点 / 每周一9点 / 每月1号9点 / 每2小时 / 每30分钟 / 每小时 / 每天
    解析成功且通过频率上限返回 cron；否则返回 None。
    """
    text = (expr or "").strip().lower()
    if not text:
        return None

    cron: Optional[str] = None
    m = _RE_DAILY_HOUR.search(text)
    if m:
        hour = _normalize_hour(int(m.group(2)), m.group(1))
        if hour is not None:
            cron = f"0 {hour} * * *"
    if cron is None:
        m = _RE_WEEKLY.search(text)
        if m:
            wd = _WEEKDAY_CN.get(m.group(1))
            hour = _normalize_hour(int(m.group(3)), m.group(2))
            if wd is not None and hour is not None:
                cron = f"0 {hour} * * {wd}"
    if cron is None:
        m = _RE_MONTHLY.search(text)
        if m:
            dom = int(m.group(1))
            hour = _normalize_hour(int(m.group(3)), m.group(2))
            if 1 <= dom <= 31 and hour is not None:
                cron = f"0 {hour} {dom} * *"
    if cron is None:
        m = _RE_EVERY_HOUR.search(text)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 23:
                cron = f"0 */{n} * * *"
    if cron is None:
        m = _RE_EVERY_MIN.search(text)
        if m:
            n = int(m.group(1))
            if n >= 1:
                cron = f"*/{n} * * * *"
    if cron is None and _RE_DAILY.search(text):
        cron = "0 9 * * *"
    if cron is None and _RE_HOURLY.search(text):
        cron = "0 * * * *"
    if cron is None:
        # 英文兜底：每周X
        for key, val in _WEEKDAY_EN.items():
            if key in text:
                hm = re.search(r"(\d{1,2})\s*[点时:]?(\d{0,2})", text)
                hour = int(hm.group(1)) if hm else 9
                if 0 <= hour <= 23:
                    cron = f"0 {hour} * * {val}"
                break

    if cron is None:
        return None
    if not _min_interval_ok(cron):
        return None
    return cron


def format_cron_human(cron_expr: str) -> str:
    """cron → 人类可读（回显确认用）。仅支持本项目产生的几种形态。"""
    parts = cron_expr.split()
    if len(parts) != 5:
        return cron_expr
    minute, hour, dom, month, dow = parts
    if dow != "*":
        names = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
        return f"每周{names.get(int(dow), dow)} {hour}:{minute:0>2}"
    if dom != "*":
        return f"每月{dom}号 {hour}:{minute:0>2}"
    if hour != "*":
        return f"每天 {hour}:{minute:0>2}"
    if minute.startswith("*/"):
        return f"每 {minute[2:]} 分钟"
    if hour.startswith("*/"):
        return f"每 {hour[2:]} 小时"
    return cron_expr
