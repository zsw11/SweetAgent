"""长期记忆数据库访问（设计文档 34-35 节）。

统一入口：
- 写连接：settings.DATABASE_URL（app_user，记忆表归它管）
- 短连接形态：每次操作 with psycopg.connect(...) as conn，FastAPI 多线程安全
- resolve_user_id：API/脚本传入的字符串 user_id（username）→ users.id（不存在则创建）
"""

from __future__ import annotations

import psycopg

from app.config.settings import settings
from app.observability.logging import get_logger

logger = get_logger("memory_db")


def connect():
    """返回一个写连接（调用方用 with 管理）。"""
    return psycopg.connect(settings.DATABASE_URL)


def resolve_user_id(user_id: str) -> int:
    """字符串 user_id（username）→ users.id；不存在则插入新用户（幂等）。

    说明：API 层 user_id 是字符串名称（如 "default"/"zhangwei"），而记忆表
    user_id 是 BIGINT REFERENCES users(id)，此处做映射。
    """
    username = (user_id or "default").strip() or "default"
    with connect() as conn:
        cur = conn.execute(
            "SELECT id FROM users WHERE username = %s", (username,)
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        conn.execute(
            "INSERT INTO users (username, display_name, email) VALUES (%s, %s, %s) "
            "ON CONFLICT (username) DO NOTHING",
            (username, username, None),
        )
        row = conn.execute(
            "SELECT id FROM users WHERE username = %s", (username,)
        ).fetchone()
        logger.info("memory.user_created", username=username, user_id=row[0])
        return row[0]
