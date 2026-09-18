"""数据库连接管理。

应用连接（DATABASE_URL）：ORM / checkpoint / 元数据管理，具备写权限；
Agent 只读连接（AGENT_DATABASE_URL）：SQL Tool 专用，见 app.tools.sql.executor。

TODO(Phase 1): 接入 SQLAlchemy engine + 连接池 + alembic。
"""
