"""全局配置：基于 pydantic-settings，读取 .env 与系统环境变量。"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（app/config/settings.py -> 上溯 3 级）
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 应用 ----------
    APP_ENV: str = "development"
    APP_DEBUG: bool = True
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # ---------- LLM Provider ----------
    LLM_DEFAULT_PROVIDER: str = "deepseek"

    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL_STRONG: str = "gpt-4o"
    OPENAI_MODEL_MEDIUM: str = "gpt-4o-mini"
    OPENAI_MODEL_SMALL: str = "gpt-4o-mini"

    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_MODEL_STRONG: str = "deepseek-chat"
    DEEPSEEK_MODEL_MEDIUM: str = "deepseek-chat"
    DEEPSEEK_MODEL_SMALL: str = "deepseek-chat"

    # ---------- PostgreSQL ----------
    DATABASE_URL: str = "postgresql://app_user:app_password@localhost:5432/sweetnight_agent"
    AGENT_DATABASE_URL: str = "postgresql://agent_reader:agent_password@localhost:5432/sweetnight_agent"

    # ---------- LangGraph Checkpoint / Memory（设计文档 33-34 节） ----------
    # langgraph-checkpoint-postgres 的表名硬编码为 checkpoints，此处声明以便配置可见
    CHECKPOINTER_TABLE: str = "checkpoints"
    STORE_TABLE_PREFIX: str = "langgraph_store"

    # ---------- 长期记忆（设计文档 34-35 节） ----------
    MEMORY_EXTRACT_THRESHOLD: int = 16_000      # 历史 token 超此值触发记忆提取（隐式信息兜底）
    HISTORY_COMPRESSION_THRESHOLD: int = 35_000  # 历史 token 超此值触发上下文压缩（预留，多轮会话时启用）
    COMPRESSION_KEEP_RECENT: int = 3             # 压缩时保留最近 N 轮原文
    MEMORY_RECALL_TOP_K: int = 5                 # 记忆向量检索 top-k 条数
    MEMORY_INJECT_TOKEN_BUDGET: int = 1_500      # 注入 Manager/部门的记忆 token 上限

    # ---------- 向量 / 知识库 ----------
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    VECTOR_DIM: int = 1536

    # ---------- Agent 运行限制（设计文档 42 节） ----------
    MAX_AGENT_ITERATIONS: int = 10
    MAX_SQL_RETRIES: int = 3
    MAX_TOOL_RETRIES: int = 3
    SQL_STATEMENT_TIMEOUT_MS: int = 30_000
    SQL_MAX_LIMIT: int = 5_000

    # ---------- Redis（可选） ----------
    REDIS_URL: str = ""

    # ---------- 可观测性（可选） ----------
    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "sweetnight-agent"


@lru_cache
def get_settings() -> Settings:
    """进程内缓存配置实例。"""
    return Settings()


settings = get_settings()
