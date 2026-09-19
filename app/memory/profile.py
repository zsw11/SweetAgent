"""用户画像 / 结构化长期记忆（设计文档 34 节）。

三张 key-value 表：
- user_profiles：画像属性（role / industry / market_scope / language …）——LLM 推断、高置信+evidence 才写
- user_preferences：用户偏好（default_market / default_currency / time_range / report_format …）——显式/可推断
- business_preferences：业务/部门级规则（scope=global/operation/finance…）——全员共享，按 scope 注入

字段设计（2026-09-20 优化，讨论见 development_log 考点二十）：
- profiles 存 confidence + evidence：写入门槛（≥0.8+evidence）已过滤，存下来供审计与未来"冲突检测"
  （新提取置信度显著更高才覆盖，否则保留旧值）
- preferences 存 evidence：latest-wins 覆盖，confidence 不存（覆盖后旧值消失，无比较对象）
- 两者都存 created_at（首次确认时间，覆盖时保留）——和 user_memories 语义对齐
- 都不存 superseded_at：key-value 天然 latest-wins，覆盖即事实；历史追溯走变更日志（待办，见文档）

写入策略：latest-wins（upsert 覆盖，key-value 语义天然如此）。
读取策略：
- get_user_memory（Manager 注入）：profiles + preferences 全量（量小）
- get_business_preferences（部门注入）：scope=本部门 ∪ global
"""

from __future__ import annotations

from typing import Any, Optional

from app.memory.db import connect, resolve_user_id
from app.observability.logging import get_logger

logger = get_logger("memory_profile")


# ---------------------------------------------------------------------------
# user_profiles / user_preferences（按 user）
# ---------------------------------------------------------------------------

def get_profiles(user_id: str) -> dict[str, str]:
    """读取用户画像（key → value）。注入用，保持轻量。"""
    uid = resolve_user_id(user_id)
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM user_profiles WHERE user_id = %s ORDER BY key",
            (uid,),
        ).fetchall()
    return {k: v for k, v in rows}


def get_preferences(user_id: str) -> dict[str, str]:
    """读取用户偏好（key → value）。注入用，保持轻量。"""
    uid = resolve_user_id(user_id)
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM user_preferences WHERE user_id = %s ORDER BY key",
            (uid,),
        ).fetchall()
    return {k: v for k, v in rows}


def get_profiles_detail(user_id: str) -> list[dict[str, Any]]:
    """读取用户画像（含 confidence/evidence/时间戳）。API 展示用。"""
    uid = resolve_user_id(user_id)
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, value, confidence, evidence, created_at, updated_at "
            "FROM user_profiles WHERE user_id = %s ORDER BY key",
            (uid,),
        ).fetchall()
    return [
        {"key": k, "value": v, "confidence": c, "evidence": e,
         "created_at": str(ct), "updated_at": str(ut)}
        for k, v, c, e, ct, ut in rows
    ]


def get_preferences_detail(user_id: str) -> list[dict[str, Any]]:
    """读取用户偏好（含 evidence/时间戳）。API 展示用。"""
    uid = resolve_user_id(user_id)
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, value, evidence, created_at, updated_at "
            "FROM user_preferences WHERE user_id = %s ORDER BY key",
            (uid,),
        ).fetchall()
    return [
        {"key": k, "value": v, "evidence": e, "created_at": str(ct), "updated_at": str(ut)}
        for k, v, e, ct, ut in rows
    ]


def get_user_memory(user_id: str) -> dict[str, dict[str, str]]:
    """Manager 注入用：画像 + 偏好全量（结构化部分）。"""
    return {"profiles": get_profiles(user_id), "preferences": get_preferences(user_id)}


def upsert_profile(user_id: str, key: str, value: str, confidence: Optional[float] = None, evidence: Optional[str] = None) -> None:
    """写入/覆盖画像属性（latest-wins）。

    created_at 保留首次确认时间；覆盖时刷新 value/confidence/evidence/updated_at。
    """
    uid = resolve_user_id(user_id)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO user_profiles (user_id, key, value, confidence, evidence, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (user_id, key) DO UPDATE SET
                value = EXCLUDED.value,
                confidence = EXCLUDED.confidence,
                evidence = EXCLUDED.evidence,
                updated_at = now()
            """,
            (uid, key, value, confidence, evidence),
        )
    logger.info(
        "memory.profile.upsert", user_id=user_id, key=key, value=str(value)[:50],
        confidence=confidence, evidence=bool(evidence),
    )


def upsert_preference(user_id: str, key: str, value: str, evidence: Optional[str] = None) -> None:
    """写入/覆盖用户偏好（latest-wins，冲突=覆盖，符合 key-value 语义）。

    created_at 保留首次确认时间；覆盖时刷新 value/evidence/updated_at。
    """
    uid = resolve_user_id(user_id)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO user_preferences (user_id, key, value, evidence, created_at, updated_at)
            VALUES (%s, %s, %s, %s, now(), now())
            ON CONFLICT (user_id, key) DO UPDATE SET
                value = EXCLUDED.value,
                evidence = EXCLUDED.evidence,
                updated_at = now()
            """,
            (uid, key, value, evidence),
        )
    logger.info(
        "memory.preference.upsert", user_id=user_id, key=key, value=str(value)[:50], evidence=bool(evidence),
    )


# ---------------------------------------------------------------------------
# business_preferences（业务/部门级，全员共享）
# ---------------------------------------------------------------------------

def get_business_preferences(scope: Optional[str] = None) -> dict[str, str]:
    """读取业务偏好。

    scope=None → 只取 global；scope=部门名 → global ∪ 该部门。
    """
    with connect() as conn:
        if scope:
            rows = conn.execute(
                "SELECT key, value, scope FROM business_preferences WHERE scope = %s OR scope = 'global' ORDER BY scope, key",
                (scope,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT key, value, scope FROM business_preferences WHERE scope = 'global' ORDER BY key",
            ).fetchall()
    return {k: v for k, v, _ in rows}


def upsert_business_preference(key: str, value: str, scope: str = "global") -> None:
    """写入/覆盖业务规则（key+scope 联合唯一，latest-wins）。"""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO business_preferences (key, value, scope, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (key, scope) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, value, scope),
        )
    logger.info("memory.business.upsert", key=key, scope=scope, value=str(value)[:50])
