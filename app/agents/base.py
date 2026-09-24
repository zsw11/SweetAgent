"""部门 Agent 基类：封装 plan→query→analyze→retry 通用内部循环。

子类只需声明配置（白名单/表映射/关键词/prompt）并实现 _load_dictionary()，
无需重复 SQL 生成、执行、repair、JSON 容错、结果组装等通用逻辑。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.config.settings import settings
from app.llm import get_chat_model, llm_available
from app.observability.logging import get_logger

logger = get_logger("base_agent")


def _get_tool_map() -> dict[str, Any]:
    """延迟获取 SQL 工具映射，避免模块加载时循环导入
    （base -> operation.tools -> operation/__init__ -> operation.agent -> base）。"""
    from app.agents.operation.tools import get_operation_tool_map
    return get_operation_tool_map()


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


def _context_text(context: Optional[dict[str, Any]]) -> str:
    """把跨部门上下文（如 Operation/Finance/Logistics 结果）转成提示文本。

    无上下文返回空串；有上下文时序列化为 JSON 文本注入 prompt。
    Product Agent 通过 {context} 占位使用；无 {context} 占位的 prompt 不受影响
    （str.format 忽略多余关键字参数）。
    """
    if not context:
        return ""
    return json.dumps(context, ensure_ascii=False, default=str)[:2000]


# 行首剥离：空白 / markdown 列表符（- * • ·）/ 数字序号（1. 2. 3.）
_BULLET_RE = re.compile(r"^[\s\-*•·\d.]+")
# 一行内多数据域分隔符：英文逗号 / 中文逗号 / 顿号 / 分号 / 句号 / 空白
_PLAN_SPLIT_RE = re.compile(r"[,，、;；。.！!？?\s]+")


def _extract_plan(text: str, known: frozenset[str]) -> list[str]:
    """从 LLM 输出中提取数据域计划（容错：markdown 列表 / 序号 / 多分隔符 / 解释文字）。

    - 每行剥离行首的 "-" / "*" / "1." 等装饰符
    - 支持一行多个数据域（逗号 / 顿号 / 分号 / 空格分隔）
    - 仅保留白名单内的数据域并去重；解释性杂词自然被白名单丢弃
    """
    plan: list[str] = []
    for ln in text.splitlines():
        ln = _BULLET_RE.sub("", ln).strip()
        if not ln:
            continue
        for part in _PLAN_SPLIT_RE.split(ln):
            p = part.strip().lower()
            if p in known and p not in plan:
                plan.append(p)
    return plan


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

    # ---- RAG（可选覆盖） ----
    # 知识库检索所属部门标签（检索时按 department 过滤）；
    # 子类设置后，数据域白名单中的 "knowledge" 将走 RAG 向量检索而非 SQL 生成。
    KNOWLEDGE_DEPARTMENT: Optional[str] = None
    KNOWLEDGE_TOP_K: int = 5

    # ---- 可选覆盖 ----
    PLAN_SYSTEM: str = "你是部门分析 Agent 的规划器，只输出数据域名称列表。"

    def __init__(self, model=None, executor=None):
        if not llm_available():
            raise RuntimeError(
                f"未配置 LLM API Key。{self.AGENT_NAME} Agent 需要 LLM，请在 .env 中配置。"
            )
        self.llm_ready = True
        self.model = model if model is not None else get_chat_model(tier="medium")
        self.tools = _get_tool_map()
        self.executor = executor or self.tools["execute_readonly_sql"]
        self.max_iterations = settings.MAX_AGENT_ITERATIONS
        self.max_sql_retries = settings.MAX_SQL_RETRIES

    # ------------------------------------------------------------------
    # 通用：规划数据域
    # ------------------------------------------------------------------
    def _plan(self, task: str, context: dict[str, Any]) -> list[str]:
        """LLM 规划数据需求，白名单过滤，保证至少含 FALLBACK_REQ。

        context 可通过 PLAN_PROMPT 中的 {context} 占位注入
        （如 Product Agent 的跨部门结论，避免重复查询其他部门数据域）。
        """
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            resp = self.model.invoke([
                SystemMessage(content=self.PLAN_SYSTEM),
                HumanMessage(content=self.PLAN_PROMPT.format(task=task, context=_context_text(context))),
            ])
            plan = _extract_plan(str(resp.content), self.KNOWN_REQS)
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
        """执行一个数据需求：生成 SQL -> 校验 -> 执行；失败/空结果自动 repair。

        knowledge 数据域不走 SQL——RAG 向量检索（见 _query_knowledge）；
        tracking 数据域不走 SQL——外部 MCP 物流轨迹（见 _query_tracking，OPT-01）。
        """
        if req == "knowledge":
            return self._query_knowledge(task)
        if req == "tracking":
            return self._query_tracking(task)
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
    # RAG：知识库向量检索（knowledge 数据域）
    # ------------------------------------------------------------------
    def _query_knowledge(self, task: str) -> dict[str, Any]:
        """RAG 检索：把任务原文向量化 → 检索本部门知识库 top-k。

        返回与 SQL 查询一致的 observations 结构（columns/rows/row_count/duration_ms/sql），
        下游 analyze 无需区分来源。
        """
        from app.knowledge.retriever import KnowledgeRetriever

        # 注意：self.executor 是 SQL 生成工具函数（输入 sql 字符串），不是 ReadOnlyExecutor 实例，
        # 因此这里不传 executor，让检索器自建只读执行器（agent_reader 角色）。
        retriever = KnowledgeRetriever(top_k=self.KNOWLEDGE_TOP_K)
        import time
        start = time.perf_counter()
        hits = retriever.search(task, department=self.KNOWLEDGE_DEPARTMENT)
        duration_ms = int((time.perf_counter() - start) * 1000)
        # 证据置信度：整体取最高分档位；完全无命中 → "none"（显式"无合适检索"信号，
        # 下游 prompt 据此禁止编造知识库内容，见 _analyze 动态规则）
        overall = hits[0]["confidence"] if hits else "none"
        rows = [
            {
                "title": h["title"],
                "source_type": h["source_type"],
                "content": h["content"],
                "similarity": h["similarity"],
                "method": h.get("method", "vector"),
                "confidence": h.get("confidence", "low"),
            }
            for h in hits
        ]
        logger.info(
            f"{self.AGENT_NAME}.knowledge.retrieve",
            department=self.KNOWLEDGE_DEPARTMENT, hits=len(rows),
            confidence=overall, duration_ms=duration_ms,
        )
        return {
            "requirement": "knowledge",
            "columns": ["title", "source_type", "content", "similarity", "method", "confidence"],
            "rows": rows,
            "row_count": len(rows),
            "duration_ms": duration_ms,
            "confidence": overall,
            "sql": f"rag:search(department={self.KNOWLEDGE_DEPARTMENT}, top_k={self.KNOWLEDGE_TOP_K})",
        }

    # ------------------------------------------------------------------
    # 外部 MCP：物流轨迹查询（tracking 数据域，OPT-01）
    # ------------------------------------------------------------------
    def _query_tracking(self, task: str) -> dict[str, Any]:
        """外部物流轨迹：MCP 协议调快递100（auto_number 识别承运商 + query_trace 查轨迹）。

        返回与 SQL/RAG 同构的 observations（columns/rows/row_count/duration_ms/sql），
        下游 analyze 无感；未配置 key / 无单号 / 查询失败时 rows 为空并降级。
        """
        from app.tools.logistics_tracking import LogisticsTrackingClient

        import time
        start = time.perf_counter()
        rows = LogisticsTrackingClient().track(task)
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            f"{self.AGENT_NAME}.tracking.mcp",
            hits=len(rows), duration_ms=duration_ms, task=task[:80],
        )
        return {
            "requirement": "tracking",
            "columns": ["tracking_no", "carrier_code", "carrier", "raw", "method", "confidence"],
            "rows": rows,
            "row_count": len(rows),
            "duration_ms": duration_ms,
            "confidence": rows[0]["confidence"] if rows else "none",
            "sql": "mcp:kuaidi100(query_trace)",
        }

    # ------------------------------------------------------------------
    # 通用：LLM 分析
    # ------------------------------------------------------------------
    def _analyze(self, task: str, observations: list[dict[str, Any]], context: Optional[dict[str, Any]] = None):
        """LLM 分析观测结果，返回 (analysis, evidence, enough, missing)。

        context 可通过 ANALYSIS_PROMPT 中的 {context} 占位注入
        （如 Product Agent 的跨部门结论，供交叉参考）。
        """
        raw = ""
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            system_text = self.ANALYSIS_PROMPT.split("任务：")[0]
            # 知识库检索无命中（confidence=none）时，显式告知 LLM：未收录就明说，禁止编造
            if any(o.get("requirement") == "knowledge" and o.get("confidence") == "none"
                   for o in observations):
                system_text += (
                    "\n\n[知识库检索规则] 本次任务中知识库检索未命中相关内容（confidence=none）。"
                    "你必须如实说明“知识库未收录相关内容”，禁止编造或猜测知识库规则内容。"
                )
            resp = self.model.invoke([
                SystemMessage(content=system_text),
                HumanMessage(content=self.ANALYSIS_PROMPT.format(
                    task=task,
                    result_json=json.dumps(observations, ensure_ascii=False, default=str),
                    context=_context_text(context),
                )),
            ])
            raw = str(resp.content)
            logger.debug(f"{self.AGENT_NAME}.analyze.llm", raw=raw[:2000])
            parsed = parse_analysis_json(raw)
            if parsed:
                # LLM 有时会把整个 JSON 塞进 summary 字段（嵌套 JSON），做二次提取
                summary = parsed.get("summary", "")
                if isinstance(summary, dict):
                    summary = summary.get("summary") or ""
                elif isinstance(summary, str) and summary.strip().startswith("{"):
                    inner = parse_analysis_json(summary)
                    if inner and inner.get("summary"):
                        summary = inner["summary"]
                analysis = [summary if isinstance(summary, str) else str(summary)]
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
