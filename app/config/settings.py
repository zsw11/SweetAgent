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

    # ---------- 多轮会话上下文（OPT-12：历史注入 / query 改写 / 压缩） ----------
    CONVERSATION_HISTORY_ENABLED: bool = True    # 总开关：是否存/读会话历史（关闭则每轮完全独立）
    CONVERSATION_KEEP_RECENT_TURNS: int = 3      # 注入上下文保留最近 N 轮原文（更早的依赖压缩摘要）
    QUERY_REWRITE_ENABLED: bool = True           # 入口是否做指代消解改写（有历史才触发，small 模型）

    # ---------- 向量 / 知识库 ----------
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    VECTOR_DIM: int = 1536

    # ---------- Agent 运行限制（设计文档 42 节） ----------
    MAX_AGENT_ITERATIONS: int = 10
    MAX_SQL_RETRIES: int = 3
    MAX_TOOL_RETRIES: int = 3
    SQL_STATEMENT_TIMEOUT_MS: int = 30_000
    SQL_MAX_LIMIT: int = 5_000

    # ---------- 质量自纠回路（考点二十九，2026-09-23） ----------
    # quality_gate 节点评估 decision 输出；不合格带 feedback 回炉重生成，耗尽后走 human-in-the-loop 或放行
    QUALITY_GATE_ENABLED: bool = True           # 总开关：False 时 decision 直接到 END（等同旧行为）
    QUALITY_GATE_JUDGE_ENABLED: bool = False    # 是否启用 LLM 裁判查跑题/漏答（规则检查永远启用；开启后每次回答多一次 small 模型调用）
    QUALITY_GATE_MAX_AUTO_RETRIES: int = 2      # 自动回炉上限；超过后若 human_in_the_loop=True 则 interrupt() 暂停，否则放行并记日志
    QUALITY_GATE_MIN_SUMMARY_LEN: int = 15      # 核心结论最短长度（低于视为无效回答）

    # ---------- 物流跟踪外部 MCP（快递100，OPT-01 落地） ----------
    # tracking 数据域走 MCP 协议查外部物流轨迹（query_trace / auto_number）；
    # 未配置 key 或连接失败时降级为空结果，不影响主链路。
    TRACKING_MCP_URL: str = "https://api.kuaidi100.com/mcp/streamable?key={key}"
    TRACKING_MCP_KEY: str = "uebKuhFT4730"          # 快递100 授权 key（api.kuaidi100.com 企业后台获取；敏感值建议放 .env）

    # ---------- Redis（可选） ----------
    REDIS_URL: str = ""

    # ---------- 可观测性（OPT-02 LangSmith，2026-09-26） ----------
    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "sweetagent"

    # ---------- 查询 / RAG 缓存（OPT-07，2026-09-27） ----------
    # SQL 结果缓存短 TTL（业务表数据随种子/运维变化，秒级过期即可止血重复查询）；
    # RAG 检索缓存长 TTL（知识库文档低频变更，靠 ingest 重灌时的表级失效钩子主动失效）。
    CACHE_ENABLED: bool = True
    SQL_CACHE_TTL_SECONDS: int = 60
    RAG_CACHE_TTL_SECONDS: int = 3600
    CACHE_MAX_ENTRIES: int = 512

    # ---------- 输入/输出安全（OPT-06，2026-09-27） ----------
    INJECTION_DETECTION_ENABLED: bool = True   # 入口提示注入检测（只标记+注入边界警告，不阻断）
    OUTPUT_MASKING_ENABLED: bool = True        # 输出 PII 脱敏（手机号/邮箱/长数字串，回答出口统一处理）


@lru_cache
def get_settings() -> Settings:
    """进程内缓存配置实例。"""
    return Settings()


settings = get_settings()
