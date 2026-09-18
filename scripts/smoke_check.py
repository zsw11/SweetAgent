"""框架冒烟检查：验证核心模块可导入、SQL 校验器工作正常。

用法（项目根目录）：
    .venv\\Scripts\\python scripts\\smoke_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

PASS = 0
FAIL = 0


def check(name: str, fn) -> None:
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  [PASS] {name}")
    except Exception as exc:  # noqa: BLE001
        FAIL += 1
        print(f"  [FAIL] {name}: {exc!r}")


def test_imports() -> None:
    """核心模块导入。"""
    import app.main  # noqa: F401
    import app.graph.state  # noqa: F401
    import app.config.settings  # noqa: F401
    import app.agents.operation.state  # noqa: F401
    import app.agents.logistics.state  # noqa: F401
    import app.agents.finance.state  # noqa: F401
    import app.agents.product.state  # noqa: F401
    import app.agents.decision.state  # noqa: F401
    import app.agents.decision.output  # noqa: F401
    import app.tools.sql.validator  # noqa: F401
    import app.tools.sql.executor  # noqa: F401
    import app.observability.logging  # noqa: F401


def test_validator() -> None:
    from app.tools.sql.validator import (
        SQLValidationError,
        add_limit,
        validate_and_bind_limit,
        validate_sql,
    )

    # 合法 SELECT + LIMIT
    ok = validate_sql("SELECT sku_id, SUM(gmv) FROM mart_sales_daily WHERE date >= '2026-01-01' GROUP BY sku_id ORDER BY 2 DESC LIMIT 10")
    assert "LIMIT" in ok

    # 无 LIMIT -> 拒绝（require_limit=True）
    try:
        validate_sql("SELECT * FROM mart_sales_daily")
        raise AssertionError("应拒绝无 LIMIT")
    except SQLValidationError:
        pass

    # DML -> 拒绝
    for bad in (
        "DELETE FROM mart_sales_daily",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DROP TABLE t",
        "TRUNCATE TABLE t",
        "COPY t FROM 'x.csv'",
        "SELECT * INTO t FROM mart_sales_daily",
        "SELECT 1; SELECT 2",  # 多语句
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT * FROM pg_catalog.pg_user",
        "SELECT * FROM information_schema.tables",
    ):
        try:
            validate_sql(bad)
            raise AssertionError(f"应拒绝: {bad}")
        except SQLValidationError:
            pass

    # LIMIT 超上限 -> 拒绝
    try:
        validate_sql("SELECT * FROM t LIMIT 99999999")
        raise AssertionError("应拒绝超限 LIMIT")
    except SQLValidationError:
        pass

    # 无 LIMIT 时注入默认 LIMIT
    bound = validate_and_bind_limit("SELECT * FROM mart_sales_daily")
    assert "LIMIT" in bound

    # add_limit 幂等
    assert "LIMIT 10" in add_limit("SELECT * FROM t LIMIT 10")


def test_fastapi() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def main() -> None:
    print("== 冒烟检查 ==")
    check("核心模块导入", test_imports)
    check("SQL 校验器", test_validator)
    check("FastAPI 健康检查", test_fastapi)
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
