"""Operation Agent 主逻辑（设计文档 11 节内部循环 / 58 节最小闭环）。

内部循环：规划数据需求 -> 生成 SQL -> 安全校验 -> 只读执行 -> 分析 ->
判断信息是否足够（不足则补充查询）-> 输出 Operation Result。

仅支持 LLM 模式（需配置 DEEPSEEK_API_KEY 等 OpenAI 兼容 Key）：
规划 / SQL 生成 / 分析均由 LLM 完成，Schema 结构 + 业务数据字典注入防编造。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.agents.operation.prompts import ANALYSIS_PROMPT, PLAN_PROMPT
from app.agents.operation.state import OperationState
from app.agents.operation.tools import get_operation_tool_map
from app.config.settings import settings
from app.llm import get_chat_model, llm_available
from app.observability.logging import get_logger

logger = get_logger("operation_agent")

# 已知数据域白名单：LLM 规划的 plan 与 retry 补的需求只能来自这里。
# 每个需求对应真实存在的表与数据管道（sales=销售 / ad=广告 / review=评论 / inventory=库存 / brand=品牌大盘）。
# 未来新增数据域时，同步扩展本集合与 _REQ_PRIORITY_TABLES。
_KNOWN_REQS: frozenset[str] = frozenset({"sales_sku", "brand_summary", "ad", "review", "inventory"})


class OperationAgent:
    """运营 Agent：内部循环查询 -> 分析 -> 判断，输出结构化结果（LLM 模式）。"""

    def __init__(self, model=None, executor=None):
        if not llm_available():
            raise RuntimeError(
                "未配置 LLM API Key（如 DEEPSEEK_API_KEY）。Operation Agent 当前仅支持 LLM 模式，"
                "请在 .env 中配置 Key 后重试。"
            )
        self.llm_ready = True
        self.model = model if model is not None else get_chat_model(tier="medium")
        self.tools = get_operation_tool_map()
        self.executor = executor or self.tools["execute_readonly_sql"]
        self.max_iterations = settings.MAX_AGENT_ITERATIONS
        self.max_sql_retries = settings.MAX_SQL_RETRIES

    # ------------------------------------------------------------------
    # 内部步骤（供 SubGraph 节点调用）
    # ------------------------------------------------------------------
    def _plan(self, task: str, context: dict[str, Any]) -> list[str]:
        """LLM 规划数据需求：输出过滤到白名单，保证至少含核心数据域 sales_sku。"""
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            resp = self.model.invoke([
                SystemMessage(content="你是运营分析 Agent 的规划器，只输出数据域名称列表。"),
                HumanMessage(content=PLAN_PROMPT.format(task=task)),
            ])
            plan = [ln.strip().lower() for ln in str(resp.content).splitlines() if ln.strip()]
            # 白名单过滤：LLM 可能输出不存在的需求名，只保留已知数据域（防编造）
            plan = [p for p in plan if p in _KNOWN_REQS]
            # 兜底：LLM 输出可能为空/全被过滤（格式错乱），保证 plan 至少含核心数据域 sales_sku，
            # 否则会"零查询直接结束"。insert(0) 使其优先被查询。
            if "sales_sku" not in plan:
                plan.insert(0, "sales_sku")
            return plan
        except Exception as exc:
            logger.warning("operation.plan.fallback", error=str(exc))
            return ["sales_sku"]

    def _generate_sql(self, req: str, task: str, context: dict[str, Any]) -> str:
        """按需求生成 SQL（LLM 模式）。"""
        sub_task = f"{task}（本次查询关注：{req}）"
        return self.tools["generate_sql"](sub_task, context, model=self.model)

    # 每个数据需求的核心表清单（优先注入，确保 LLM 使用正确口径的表）
    _REQ_PRIORITY_TABLES: dict[str, list[str]] = {
        "sales_sku": ["mart_sales_daily", "sales_daily", "product_skus", "brands"],
        "brand_summary": ["mart_sales_daily", "brands"],
        "ad": ["ad_performance_daily", "ad_campaigns", "product_skus", "brands"],
        "review": ["reviews", "product_skus", "brands"],
        "inventory": ["inventory_daily", "product_skus", "brands"],
    }

    def _build_schema_context(self, req: str, base: dict[str, Any]) -> dict[str, Any]:
        """用 Schema 工具定位真实表，构建 schema 上下文 + 业务数据字典（设计文档 20 节）。

        关键：绝不让 LLM 凭记忆编表名/字段；品牌/市场/SKU 等过滤必须基于真实取值。
        """
        # 需求名 -> schema_search 搜索词（LLM 模式定位真实表用）。
        # 例：sales_sku 搜 "sku" 比搜 "sales_sku" 更容易命中 product_skus；
        # .get(req, req)：未映射的新需求直接用需求名本身搜索。
        keyword = {
            "sales_sku": "sku",
            "brand_summary": "brand",
            "ad": "campaign",
            "review": "review",
            "inventory": "inventory",
        }.get(req, req)
        try:
            # 1) 优先表清单（保证核心表进入上下文）
            table_names: list[str] = []
            for t in self._REQ_PRIORITY_TABLES.get(req, []):
                if t not in table_names:
                    table_names.append(t)
            # 2) schema_search 命中的候选表补充（去重）
            hits = self.tools["schema_search"](keyword, limit=8)
            for h in hits:
                if h["table_name"] not in table_names:
                    table_names.append(h["table_name"])
            # 3) 取真实 schema
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
            logger.warning("operation.schema_context.fail", error=str(exc))
            tables, metrics, dictionary = [], [], ""
        return {**base, "tables": tables, "metrics": metrics, "dictionary": dictionary}

    def _load_dictionary(self) -> str:
        """加载业务数据字典（品牌映射 / 市场 / SKU 样例 / 时间窗口 + 查询提示）。"""
        lines: list[str] = []
        try:
            r = self.executor("SELECT id, name FROM brands ORDER BY id LIMIT 20")
            lines.append("品牌: " + ", ".join(f"{row['id']}={row['name']}" for row in r["rows"]))
        except Exception:
            pass
        try:
            r = self.executor("SELECT DISTINCT country FROM sales_daily ORDER BY 1 LIMIT 20")
            lines.append("市场取值: " + ", ".join(str(row["country"]) for row in r["rows"]))
        except Exception:
            pass
        try:
            r = self.executor("SELECT sku_code FROM product_skus ORDER BY id LIMIT 10")
            lines.append("SKU 编码样例: " + ", ".join(str(row["sku_code"]) for row in r["rows"]) + "（SKU 不含品牌名）")
        except Exception:
            pass
        try:
            r = self.executor("SELECT MIN(date) AS d0, MAX(date) AS d1 FROM sales_daily")
            row = r["rows"][0] if r["rows"] else {}
            lines.append(f"数据时间窗口: {row.get('d0')} ~ {row.get('d1')}（共约 90 天；last21 = 最后21天）")
        except Exception:
            pass
        lines.append(
            "查询提示: ①品牌过滤用 JOIN brands 按 name 匹配（如 brands.name='SweetNight'），"
            "不要用 sku_code 匹配品牌名；②时间分段用 date > (SELECT MAX(date) - 21 FROM 主表) 区分 last21/prev；"
            "③prev 段约 69 天、last21 段约 21 天，天数不同——对比必须换算为日均"
            "（如 SUM(units)/COUNT(DISTINCT date) 输出 daily_units），禁止直接比较两段总量；"
            "④聚合查询应能返回行，若过滤过严返回空请放宽条件。"
        )
        text = "\n".join(lines)
        logger.debug("operation.dictionary", text=text[:1500])
        return text

    def _query_one(self, req: str, task: str, context: dict[str, Any]) -> dict[str, Any]:
        """执行一个数据需求：生成 SQL -> 校验 -> 执行；执行失败/空结果自动修复重试（设计文档 40 节 B 类）。

        Returns:
            observation dict（含 requirement / columns / rows / row_count / duration_ms / sql）。
        """
        schema_ctx = self._build_schema_context(req, context)
        sql = self._generate_sql(req, task, schema_ctx)
        logger.debug("operation.query.sql", requirement=req, sql=sql)
        last_err: Optional[BaseException] = None
        for attempt in range(self.max_sql_retries):
            try:
                result = self.executor(sql)
                # 聚合查询返回 0 行多半是过滤条件错误（品牌/时间/市场），回喂 LLM 修复一次
                if result.get("row_count", 0) == 0 and attempt < self.max_sql_retries - 1:
                    logger.warning("operation.query.empty.repair", requirement=req, attempt=attempt + 1)
                    sql = self.tools["repair_sql"](
                        sql,
                        "查询返回 0 行。这是聚合查询不应为空，可能是品牌/时间/市场过滤条件错误，"
                        "请依据数据字典修正（例如品牌用 brands.name 匹配、时间用 MAX(date) 窗口）。",
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
                    logger.warning("operation.query.repair", requirement=req, attempt=attempt + 1, error=str(exc))
                    sql = self.tools["repair_sql"](sql, str(exc), schema_ctx, model=self.model)
        raise RuntimeError(f"需求 {req} 查询失败（{self.max_sql_retries} 次重试）: {last_err}")

    def _analyze(self, task: str, observations: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]], bool, list[str]]:
        """LLM 分析观测结果。返回 (analysis, evidence, enough, missing)。"""
        raw = ""
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            resp = self.model.invoke([
                SystemMessage(content=ANALYSIS_PROMPT.split("任务：")[0]),
                HumanMessage(content=ANALYSIS_PROMPT.format(task=task, result_json=json.dumps(observations, ensure_ascii=False, default=str))),
            ])
            raw = str(resp.content)
            logger.debug("operation.analyze.llm", raw=raw[:2000])
            parsed = _parse_analysis_json(raw)
            if parsed:
                analysis = [parsed.get("summary", "")]
                evidence = [{"type": "analysis", **parsed}]
                # 结构化提取 LLM 输出的指标 / 异常，转成统一 evidence 供下游消费
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
            logger.warning("operation.analyze.fallback", error=str(exc))
        # LLM 调用失败 / 输出无法解析：保留原文供追溯，标记足够结束（避免 analyze->retry 死循环）
        analysis = [raw[:2000]] if raw else ["LLM 分析失败，无可用结论（详见日志 operation.analyze.fallback）。"]
        return analysis, [{"type": "analysis", "summary": analysis[0], "raw": raw[:2000]}], True, []

    def _build_result(self, task, observations, sql_history, analysis, evidence) -> dict[str, Any]:
        """组装 Operation Result（供 Decision Agent 消费）。"""
        if not analysis:
            analysis = ["未产生分析结论（数据不足）。"]
        return {
            "agent": "operation",
            "task": task,
            "summary": analysis[0],
            "analysis": analysis,
            "metrics": _collect_metrics(evidence),
            "anomalies": _collect_anomalies(evidence),
            "evidence": evidence,
            "observations": observations,
            "sql_history": sql_history,
            "confidence": 0.9 if observations else 0.1,
            "mode": "llm",
        }


# ---------------------------------------------------------------------------
# 分析结果辅助函数
# ---------------------------------------------------------------------------

def _collect_metrics(evidence) -> list[dict[str, Any]]:
    """把 evidence 中的 llm_metric 转成标准化指标（LLM 模式唯一来源）。"""
    return [
        {"name": e["name"], "prev": e.get("prev"), "last21": e.get("last21"), "change_pct": e.get("change_pct")}
        for e in evidence
        if e.get("type") == "llm_metric" and "name" in e
    ]


def _collect_anomalies(evidence) -> list[dict[str, Any]]:
    """把 evidence 中的 llm_anomaly 汇总为统一结构（按 change_pct 升序：负向最严重在前）。"""
    out = [
        {"sku": e["sku"], "indicator": e.get("indicator", "综合"), "change_pct": e.get("change_pct")}
        for e in evidence
        if e.get("type") == "llm_anomaly" and e.get("sku")
    ]
    return sorted(out, key=lambda a: a.get("change_pct") or 0)


def _parse_analysis_json(text: str) -> Optional[dict[str, Any]]:
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


# 供 graph.py 复用的 state 构建辅助
def initial_state(task: str, context: Optional[dict[str, Any]] = None) -> OperationState:
    """构造 OperationState 初始值。"""
    return {
        "task": task,
        "messages": [],
        "iteration": 0,
        "tool_calls": [],
        "sql_history": [],
        "observations": [],
        "analysis": [],
        "evidence": [],
        "final_result": None,
        "error": None,
        "sql_retry_count": 0,
        "tool_retry_count": 0,
        "max_iterations": settings.MAX_AGENT_ITERATIONS,
        "sales_context": context or {},
    }
