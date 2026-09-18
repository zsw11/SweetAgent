"""一键初始化数据库脚本（幂等，可重复运行）。

用法（项目根目录）：
    .venv\\Scripts\\python scripts\\init_database.py            # 仅初始化表结构
    .venv\\Scripts\\python scripts\\init_database.py --seed     # 初始化 + 灌入 90 天种子数据

执行流程（对应 db/ 下 SQL 文件，全部幂等）：
    1. 创建角色（app_user 写 / agent_reader 只读）   db/00-roles.sql
    2. 创建数据库 sweetnight_agent（如不存在）
    3. 启用扩展（vector / pg_trgm）                   db/01-extensions.sql
    4. 建表（67 张，属主 app_user）                   db/02-schema.sql
    5. Agent 只读授权                                 db/03-grants.sql
    6. [可选] 种子数据                                scripts/seed_data.py

连接参数可用环境变量覆盖（默认值对应本地开发容器 langgraph-postgres）：
    DB_HOST=localhost  DB_PORT=5432  DB_NAME=sweetnight_agent
    DB_SUPERUSER=langgraph_user  DB_SUPERUSER_PASSWORD=123456
    APP_DB_USER=app_user  APP_DB_PASSWORD=app_password
    AGENT_DB_USER=agent_reader  AGENT_DB_PASSWORD=agent_password
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_DIR = PROJECT_ROOT / "db"


def _env(key: str, default: str) -> str:
    return os.getenv(key, default)


HOST = _env("DB_HOST", "localhost")
PORT = _env("DB_PORT", "5432")
DB_NAME = _env("DB_NAME", "sweetnight_agent")

SUPERUSER = _env("DB_SUPERUSER", "langgraph_user")
SUPERUSER_PASSWORD = _env("DB_SUPERUSER_PASSWORD", "123456")

APP_USER = _env("APP_DB_USER", "app_user")
APP_PASSWORD = _env("APP_DB_PASSWORD", "app_password")

AGENT_USER = _env("AGENT_DB_USER", "agent_reader")
AGENT_PASSWORD = _env("AGENT_DB_PASSWORD", "agent_password")


def _dsn(user: str, password: str, db: str) -> str:
    return f"postgresql://{user}:{password}@{HOST}:{PORT}/{db}"


def run_sql_file(conn, path: Path) -> None:
    """以简单查询协议执行 SQL 文件（支持多语句 / DO 块）。"""
    sql_text = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql_text)


def main() -> int:
    seed = "--seed" in sys.argv

    print(f"[1/6] 创建角色（{APP_USER} 写 / {AGENT_USER} 只读）...")
    with psycopg.connect(_dsn(SUPERUSER, SUPERUSER_PASSWORD, "postgres"), autocommit=True) as conn:
        run_sql_file(conn, DB_DIR / "00-roles.sql")

    print(f"[2/6] 检查数据库 {DB_NAME} ...")
    with psycopg.connect(_dsn(SUPERUSER, SUPERUSER_PASSWORD, "postgres"), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
            exists = cur.fetchone() is not None
        if not exists:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8'").format(
                        sql.Identifier(DB_NAME), sql.Identifier(APP_USER)
                    )
                )
            print(f"    已创建数据库 {DB_NAME}（属主 {APP_USER}）")
        else:
            print(f"    数据库 {DB_NAME} 已存在，跳过")

    print("[3/6] 启用扩展（vector / pg_trgm）...")
    with psycopg.connect(_dsn(SUPERUSER, SUPERUSER_PASSWORD, DB_NAME), autocommit=True) as conn:
        run_sql_file(conn, DB_DIR / "01-extensions.sql")

    print(f"[4/6] 建表（db/02-schema.sql，属主 {APP_USER}）...")
    with psycopg.connect(_dsn(APP_USER, APP_PASSWORD, DB_NAME), autocommit=True) as conn:
        run_sql_file(conn, DB_DIR / "02-schema.sql")

    print("[5/6] Agent 只读授权（db/03-grants.sql）...")
    with psycopg.connect(_dsn(SUPERUSER, SUPERUSER_PASSWORD, DB_NAME), autocommit=True) as conn:
        run_sql_file(conn, DB_DIR / "03-grants.sql")

    with psycopg.connect(_dsn(APP_USER, APP_PASSWORD, DB_NAME)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
            )
            n_tables = cur.fetchone()[0]
    print(f"    完成：public schema 共 {n_tables} 张表")

    if seed:
        print("[6/6] 灌入种子数据（scripts/seed_data.py）...")
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "seed_data.py")],
            check=True,
        )
    else:
        print("[6/6] 跳过种子数据（如需灌数据：python scripts\\init_database.py --seed）")

    print("\n初始化完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
