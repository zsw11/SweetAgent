"""能力域注册表（NL2Cron 唯一权威源，开发日志考点六十四）。

设计要点：
- 模板粒度 = 能力域，不是具体任务——用户语言映射到能力域（宽），具体业务是参数（自由）
- 每个能力域注册：参数 Schema（Pydantic 模型，LLM 解析出的 params 必须过）+ 风险等级 + 执行 handler
- LLM 只能从本注册表选 domain，不能发明任务；params 过 Schema 校验才落库
- handler 全部复用现有链路（run_question 主图 / 只读 SQL / ingest / evaluation），
  不新增"任意代码执行"面——安全边界 = 注册表白名单 + 参数 Schema
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from app.observability.logging import get_logger

logger = get_logger("scheduler.registry")


# ============================================================
# 能力域参数 Schema（params 必须匹配对应模型，校验失败拒绝）
# ============================================================

class AnalysisParams(BaseModel):
    """经营分析域参数：定时跑主图分析任意问题。"""
    question: str = Field(..., min_length=4, max_length=500, description="要定时分析的问题")
    market: Optional[str] = Field(None, description="市场过滤（US/CA/UK 等，可空）")
    brand: Optional[str] = Field(None, description="品牌过滤（可空）")


class MonitorParams(BaseModel):
    """异常监控域参数：定时跑规则 SQL 检查指标。"""
    metric: str = Field(..., description="监控指标（sales_gmv/inventory_days/roas/refund_rate 等）")
    threshold: float = Field(..., description="告警阈值（数值）")
    direction: str = Field("below", description="告警方向（below/above）")
    market: Optional[str] = Field(None, description="市场过滤（可空）")


class KnowledgeSyncParams(BaseModel):
    """知识同步域参数：定时增量 ingest。"""
    source_dir: str = Field("", description="文档源目录（空=默认知识目录）")


class EvalRegressionParams(BaseModel):
    """质量回归域参数：定时跑评估。"""
    case_ids: str = Field("", description="用例 ID 列表（逗号分隔，空=默认 6,10）")


# 能力域定义（唯一权威源）
class CapabilityDomain:
    """一个能力域 = 参数 Schema + 风险等级 + 执行 handler + 描述。"""

    def __init__(
        self,
        key: str,
        name: str,
        description: str,
        params_schema: type[BaseModel],
        risk_level: str,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
        examples: Optional[list[str]] = None,
    ) -> None:
        self.key = key
        self.name = name
        self.description = description
        self.params_schema = params_schema
        self.risk_level = risk_level          # low/mid/high
        self.handler = handler
        self.examples = examples or []        # 检索面：用户语言示例（意图匹配用）


# ============================================================
# 能力域 handlers（全部复用现有链路）
# ============================================================

def _handler_analysis(params: dict[str, Any]) -> dict[str, Any]:
    """经营分析域：跑主图 run_question。"""
    from app.graph.main_graph import run_question

    question = params.get("question", "")
    result = run_question(question, thread_id=f"cron-analysis-{abs(hash(question)) % 10 ** 6}", user_id="scheduler")
    return {"ok": True, "final_answer": result.get("final_answer", "")[:2000]}


def _handler_monitor(params: dict[str, Any]) -> dict[str, Any]:
    """异常监控域：跑规则 SQL（只读 agent_reader 角色）。"""
    from app.tools.sql.executor import ReadOnlyExecutor

    metric = params.get("metric", "")
    threshold = params.get("threshold", 0.0)
    direction = params.get("direction", "below")
    market = params.get("market")
    # 指标 → SQL 映射（白名单，防任意 SQL 注入——只允许注册过的指标）
    _METRIC_SQL = {
        "inventory_days": (
            "SELECT sku_id, stock_days FROM mart_inventory_risk "
            "WHERE date = (SELECT MAX(date) FROM mart_inventory_risk) "
            "AND stock_days < {t}"
        ),
        "roas": (
            "SELECT campaign_id, roas FROM mart_ad_performance_daily "
            "WHERE date = (SELECT MAX(date) FROM mart_ad_performance_daily) "
            "AND roas < {t}"
        ),
        "sales_gmv": (
            "SELECT sku_id, gmv FROM mart_sales_daily "
            "WHERE date >= CURRENT_DATE - INTERVAL '21 days' "
            "GROUP BY sku_id, gmv ORDER BY gmv ASC LIMIT 10"
        ),
    }
    sql_template = _METRIC_SQL.get(metric)
    if sql_template is None:
        return {"ok": False, "error": f"unknown_metric:{metric}"}
    if market:
        sql = sql_template.replace("{t}", str(threshold)) + f" AND country = '{market}'"
    else:
        sql = sql_template.replace("{t}", str(threshold))
    executor = ReadOnlyExecutor()
    try:
        result = executor.execute(sql)
        rows = result.get("rows", [])
        return {"ok": True, "hit_count": len(rows), "rows": rows[:20]}
    except Exception as exc:  # 执行失败交由 service 层重试/审计
        return {"ok": False, "error": str(exc)}


def _handler_knowledge_sync(params: dict[str, Any]) -> dict[str, Any]:
    """知识同步域：增量 ingest（content_hash 幂等）。

    当前为占位实现：定时重灌内置种子文档（KNOWLEDGE_DOCS），
    仅保证种子文档始终在库（防误删/防漏灌），重复执行靠 content_hash 幂等不产生重复行。
    用户上传文档走既有 ingest 接口（主动动作，本域不负责）。

    TODO(真实落地)：source_dir 参数预留了"文档目录增量同步"通道，实现时改为：
    1. 扫描 source_dir 下新增文件（按 mtime/文件名 hash 记录增量游标）
    2. 解析切块 → ingest_many 幂等入库，跳过已 ingest 文件
    3. 仍保持只读源头 + 幂等写入，不扩展任意执行能力
    """
    from app.knowledge.ingest import ingest_many
    from app.memory.db import connect

    source_dir = params.get("source_dir", "")
    # 简化：读取默认知识文档种子（真实场景接文档目录扫描）
    from app.knowledge.seed_docs import KNOWLEDGE_DOCS

    docs = KNOWLEDGE_DOCS if not source_dir else KNOWLEDGE_DOCS
    with connect() as conn:
        stats = ingest_many(conn, docs)
    return {"ok": True, "stats": stats}


def _handler_eval_regression(params: dict[str, Any]) -> dict[str, Any]:
    """质量回归域：以子进程跑评估脚本（scripts/run_evaluation.py 入口是 argparse main）。"""
    import subprocess
    import sys
    from pathlib import Path

    case_ids = params.get("case_ids", "") or "6,10"
    root = Path(__file__).resolve().parents[3]
    cmd = [sys.executable, str(root / "scripts" / "run_evaluation.py"), "--case", str(case_ids)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return {"ok": proc.returncode == 0,
            "result": (proc.stdout or proc.stderr)[-1500:]}


# ============================================================
# 注册表本体（唯一权威源：NL2Cron 解析只能从这选 domain）
# ============================================================

CAPABILITY_DOMAINS: dict[str, CapabilityDomain] = {
    d.key: d
    for d in [
        CapabilityDomain(
            key="analysis",
            name="经营分析",
            description="定时跑多 Agent 主图分析任意业务问题（销量/GMV/异常SKU/产品策略等）",
            params_schema=AnalysisParams,
            risk_level="mid",  # 跑主图烧 LLM token，属中风险
            handler=_handler_analysis,
            examples=["每天早上9点分析美国市场销量", "每周一生成运营周报", "定时分析异常SKU"],
        ),
        CapabilityDomain(
            key="monitor",
            name="异常监控",
            description="定时跑规则 SQL 监控指标阈值（库存天数/ROAS/销量），命中告警",
            params_schema=MonitorParams,
            risk_level="low",  # 只读 SQL，低风险
            handler=_handler_monitor,
            examples=["每天看库存天数低于12天的SKU", "监控广告ROAS有没有低于2", "下班前看销量异常"],
        ),
        CapabilityDomain(
            key="knowledge_sync",
            name="知识同步",
            description="定时增量同步知识库（content_hash 幂等去重）",
            params_schema=KnowledgeSyncParams,
            risk_level="low",
            handler=_handler_knowledge_sync,
            examples=["每天同步新文档到知识库"],
        ),
        CapabilityDomain(
            key="eval_regression",
            name="质量回归",
            description="定时跑 Agent 评估用例，跟踪路由/SQL/答案准确率与费用趋势",
            params_schema=EvalRegressionParams,
            risk_level="mid",  # 跑评估烧 token
            handler=_handler_eval_regression,
            examples=["每周一跑一遍评估", "定时回归测试Agent质量"],
        ),
    ]
}


def get_domain(key: str) -> Optional[CapabilityDomain]:
    """按 key 取能力域；不存在返回 None（白名单外一律拒绝）。"""
    return CAPABILITY_DOMAINS.get(key)


def list_domains() -> list[dict[str, Any]]:
    """返回能力域目录（给用户展示/引导选择用）。"""
    return [
        {
            "key": d.key,
            "name": d.name,
            "description": d.description,
            "risk_level": d.risk_level,
            "examples": d.examples[:3],
        }
        for d in CAPABILITY_DOMAINS.values()
    ]
