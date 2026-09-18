"""部门 Agent 基类：封装 plan→query→analyze→retry 通用内部循环。

子类只需声明配置（白名单/表映射/关键词/prompt）并实现 _load_dictionary()，
无需重复 SQL 生成、执行、repair、JSON 容错、结果组装等通用逻辑。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.agents.operation.tools import get_operation_tool_map
from app.config.settings import settings
from app.llm import get_chat_model, llm_available
from app.observability.logging import get_logger

logger = get_logger("base_agent")


# ---------------------------------------------------------------------------
# 通用辅助函数（供基类和子类复用）
# ---------------------------------------------------------------------------

def parse_analysis_json(text: str) -> Optional[dict[str, Any]]:
    """从 LLM 输出中提取 JSON（容忍 markdown 代码块包裹）。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:].strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def collect_metrics(evidence) -> list[dict[str, Any]]:
    """把 evidence 中的 llm_metric 转成标准化指标。"""
    return [
        {"name": e["name"], "prev": e.get("prev"), "last21": e.get("last21"), "change_pct": e.get("change_pct")}
        for e in evidence
        if e.get("type") == "llm_metric" and "name" in e
    ]


def collect_anomalies(evidence) -> list[dict[str, Any]]:
    """把 evidence 中的 llm_anomaly 汇总（按 change_pct 升序）。"""
    out = [
        {"sku": e["sku"], "indicator": e.get("indicator", "综合"), "change_pct": e.get("change_pct")}
        for e in evidence
        if e.get("type") == "llm_anomaly" and e.get("sku")
    ]
    return sorted(out, key=lambda a: a.get("change_pct") or 0)


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------

class BaseDepartmentAgent:
    """部门 Agent 基类：封装通用内部循环（plan→query→analyze→retry）。

    子类必须声明以下类属性：
        AGENT_NAME: str               部门名（operation/finance/logistics）
        KNOWN_REQS: frozenset[str]    数据域白名单
        PRIORITY_TABLES: dict         数据域→核心表列表
        KEYWORD_MAP: dict             数据域→schema_search 关键词
        FALLBACK_REQ: str             plan 为空时的兜底数据域
        PLAN_PROMPT: str              规划 prompt（含 {task} 占位）
        ANALYSIS_PROMPT: str          分析 prompt（含 {task} 和 {result_json} 占位）

    子类必须实现：
        _load_dictionary() -> str     业务数据字典
    """

    # ---- 子类必须覆盖的类属性 ----
    AGENT_NAME: str = "base"
    KNOWN_REQS: frozenset[str] = frozenset()
    PRIORITY_TABLES: dict[str, list[str]] = {}
    KEYWORD_MAP: dict[str, str] = {}
    FALLBACK_REQ: str = ""
    PLAN_PROMPT: str = ""
    ANALYSIS_PROMPT: str = ""

    # ---- 可选覆盖 ----
    PLAN_SYSTEM: str = "你是部门分析 Agent 的规划器，只输出数据域名称列表。"

    def __init__(self, model=None, executor=None):
        if not llm_available():
            raise RuntimeError(
                f"未配置 LLM API Key。{self.AGENT_NAME} Agent 需要 LLM，请在 .env 中配置。"
            )
        self.llm_ready = True
        self.model = model if model is not None else get_chat_model(tier="medium")
        self.tools = get_operation_tool_map()
        self.executor = executor or self.tools["execute_readonly_sql"]
        self.max_iterations = settings.MAX_AGENT_ITERATIONS
        self.max_sql_retries = settings.MAX_SQL_RETRIES

    # ------------------------------------------------------------------
    # 通用：规划数据域
    # ------------------------------------------------------------------
    def _plan(self, task: str, context: dict[str, Any]) -> list[str]:
        """LLM 规划数据需求，白名单过滤，保证至少含 FALLBACK_REQ。"""
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            resp = self.model.invoke([
                SystemMessage(content=self.PLAN_SYSTEM),
                HumanMessage(content=self.PLAN_PROMPT.format(task=task)),
            ])
            plan = [ln.strip().lower() for ln in str(resp.content).splitlines() if ln.strip()]
            plan = [p for p in plan if p in self.KNOWN_REQS]
            if self.FALLBACK_REQ not in plan:
                plan.insert(0, self.FALLBACK_REQ)
            return plan
        except Exception as exc:
            logger.warning(f"{self.AGENT_NAME}.plan.fallback", error=str(exc))
            return [self.FALLBACK_REQ]

    # ------------------------------------------------------------------
    # 通用：构建 schema 上下文
    # ------------------------------------------------------------------
    def _build_schema_context(self, req: str, base: dict[str, Any]) -> dict[str, Any]:
        """用优先级表 + schema_search 定位真实表，构建 schema 上下文 + 数据字典。"""
        keyword = self.KEYWORD_MAP.get(req, req)
        try:
            table_names: list[str] = []
            for t in self.PRIORITY_TABLES.get(req, []):
                if t not in table_names:
                    table_names.append(t)
            hits = self.tools["schema_search"](keyword, limit=8)
            for h in hits:
                if h["table_name"] not in table_names:
                    table_names.append(h["table_name"])
            tables = []
            for name in table_names[:6]:
                try:
                    t = self.tools["get_table_schema"](name)
                    if not t.get("error"):
                        tables.append(t)
                except Exception:
                    continue
            metrics = self.tools["metric_definition"]()
            dictionary = self._load_dictionary()
        except Exception as exc:
            logger.warning(f"{self.AGENT_NAME}.schema_context.fail", error=str(exc))
            tables, metrics, dictionary = [], [], ""
        return {**base, "tables": tables, "metrics": metrics, "dictionary": dictionary}

    # ------------------------------------------------------------------
    # 通用：单需求查询（生成SQL→校验→执行→repair）
    # ------------------------------------------------------------------
    def _generate_sql(self, req: str, task: str, context: dict[str, Any]) -> str:
        sub_task = f"{task}（本次查询关注：{req}）"
        return self.tools["generate_sql"](sub_task, context, model=self.model)

    def _query_one(self, req: str, task: str, context: dict[str, Any]) -> dict[str, Any]:
        """执行一个数据需求：生成 SQL -> 校验 -> 执行；失败/空结果自动 repair。"""
        schema_ctx = self._build_schema_context(req, context)
        sql = self._generate_sql(req, task, schema_ctx)
        logger.debug(f"{self.AGENT_NAME}.query.sql", requirement=req, sql=sql)
        last_err = None
        for attempt in range(self.max_sql_retries):
            try:
                result = self.executor(sql)
                if result.get("row_count", 0) == 0 and attempt < self.max_sql_retries - 1:
                    logger.warning(f"{self.AGENT_NAME}.query.empty.repair", requirement=req, attempt=attempt + 1)
                    sql = self.tools["repair_sql"](
                        sql,
                        "查询返回 0 行。这是聚合查询不应为空，可能是过滤条件错误，"
                        "请依据数据字典修正。",
                        schema_ctx,
                        model=self.model,
                    )
                    continue
                return {
                    "requirement": req,
                    "columns": result.get("columns", []),
                    "rows": result.get("rows", []),
                    "row_count": result.get("row_count", 0),
                    "duration_ms": result.get("duration_ms", 0),
                    "sql": sql,
                }
            except Exception as exc:
                last_err = exc
                if attempt < self.max_sql_retries - 1:
                    logger.warning(f"{self.AGENT_NAME}.query.repair", requirement=req, attempt=attempt + 1, error=str(exc))
                    sql = self.tools["repair_sql"](sql, str(exc), schema_ctx, model=self.model)
        raise RuntimeError(f"需求 {req} 查询失败（{self.max_sql_retries} 次重试）: {last_err}")

    # ------------------------------------------------------------------
    # 通用：LLM 分析
    # ------------------------------------------------------------------
    def _analyze(self, task: str, observations: list[dict[str, Any]]):
        """LLM 分析观测结果，返回 (analysis, evidence, enough, missing)。"""
        raw = ""
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            resp = self.model.invoke([
                SystemMessage(content=self.ANALYSIS_PROMPT.split("任务：")[0]),
                HumanMessage(content=self.ANALYSIS_PROMPT.format(
                    task=task,
                    result_json=json.dumps(observations, ensure_ascii=False, default=str),
                )),
            ])
            raw = str(resp.content)
            logger.debug(f"{self.AGENT_NAME}.analyze.llm", raw=raw[:2000])
            parsed = parse_analysis_json(raw)
            if parsed:
                analysis = [parsed.get("summary", "")]
                evidence = [{"type": "analysis", **parsed}]
                for m in parsed.get("metrics") or []:
                    if isinstance(m, dict):
                        evidence.append({"type": "llm_metric", **m})
                for a in parsed.get("anomalies") or []:
                    if isinstance(a, dict):
                        evidence.append({"type": "llm_anomaly", **a})
                enough = bool(parsed.get("enough", True))
                missing = [str(m) for m in parsed.get("missing", [])] if isinstance(parsed.get("missing"), list) else []
                return analysis, evidence, enough, missing
        except Exception as exc:
            logger.warning(f"{self.AGENT_NAME}.analyze.fallback", error=str(exc))
        analysis = [raw[:2000]] if raw else ["LLM 分析失败，无可用结论。"]
        return analysis, [{"type": "analysis", "summary": analysis[0], "raw": raw[:2000]}], True, []

    # ------------------------------------------------------------------
    # 通用：组装结果
    # ------------------------------------------------------------------
    def _build_result(self, task, observations, sql_history, analysis, evidence) -> dict[str, Any]:
        """组装部门 Result（供 Decision Agent 消费）。"""
        if not analysis:
            analysis = ["未产生分析结论（数据不足）。"]
        return {
            "agent": self.AGENT_NAME,
            "task": task,
            "summary": analysis[0],
            "analysis": analysis,
            "metrics": collect_metrics(evidence),
            "anomalies": collect_anomalies(evidence),
            "evidence": evidence,
            "observations": observations,
            "sql_history": sql_history,
            "confidence": 0.9 if observations else 0.1,
            "mode": "llm",
        }

    # ------------------------------------------------------------------
    # 子类必须实现
    # ------------------------------------------------------------------
    def _load_dictionary(self) -> str:
        """加载业务数据字典（品牌/市场/SKU/时间窗口/查询提示）。"""
        raise NotImplementedError
