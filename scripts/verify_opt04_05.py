# -*- coding: utf-8 -*-
"""OPT-04 结构化输出 + OPT-05 Function Calling 原生化 验证（2026-09-24）。

场景（全部 mock，不依赖真实 LLM/DB）：
  A. generator：tool_calls 通道提取 SQL（generate_sql / repair_sql）
  B. generator：模型不支持 tool_calls -> 文本降级
  C. base._analyze：with_structured_output 成功 -> 同构 result
  D. base._analyze：结构化失败 -> 降级旧 parse_analysis_json 路径
  E. decision.run：结构化成功 -> DecisionOutput dict
  F. decision.run：结构化失败 -> 降级旧解析链
  G.（可选，需真实 key）DeepSeek 真实 bind_tools 冒烟

运行：.venv\\Scripts\\python -u scripts\\verify_opt04_05.py
"""
import sys

sys.path.insert(0, ".")

from langchain_core.messages import AIMessage  # noqa: E402


# ----------------------------------------------------------------------
# FakeModel：模拟 ChatOpenAI 的 bind_tools / with_structured_output / invoke
# ----------------------------------------------------------------------

class _FakeStructured:
    def __init__(self, model, schema):
        self._model = model
        self._schema = schema

    def invoke(self, messages):
        mode = self._model.mode
        if mode == "structured_analysis":
            return self._schema(
                summary="库存风险中等", metrics=[{"name": "stock_days", "prev": 15, "last21": 9, "change_pct": -0.4}],
                anomalies=[{"sku": "S001", "indicator": "stock_days", "change_pct": -0.4}],
                confidence=0.8, enough=True, missing=[],
            )
        if mode == "structured_decision":
            return self._schema(
                summary="建议补货", findings=[{"category": "logistics", "finding": "库存不足"}],
                root_causes=[{"cause": "补货周期长", "evidence": "在途 10 天"}],
                recommendations=[{"priority": "P0", "action": "加急补货"}],
                risks=[{"risk": "断货", "severity": "high"}], confidence=0.75,
            )
        if mode == "structured_fail":
            raise RuntimeError("structured output not supported")
        raise RuntimeError(f"unexpected mode: {mode}")


class FakeModel:
    def __init__(self, mode="tool_calls"):
        self.mode = mode
        self.model_name = "fake-model"

    def bind_tools(self, tools):
        if self.mode == "tools_fail":
            raise RuntimeError("API does not support tools")
        return self

    def with_structured_output(self, schema, method="function_calling", **kwargs):
        return _FakeStructured(self, schema)

    def invoke(self, messages):
        if self.mode == "tool_calls":
            return AIMessage(content="", tool_calls=[
                {"name": "generate_sql", "args": {"sql": "SELECT 1 AS x LIMIT 1"}, "id": "c1"}])
        if self.mode == "repair_tool_calls":
            return AIMessage(content="", tool_calls=[
                {"name": "repair_sql", "args": {"fixed_sql": "SELECT 2 AS y LIMIT 1"}, "id": "c2"}])
        if self.mode == "text_sql":
            return AIMessage(content="SELECT 3 AS z LIMIT 1")
        if self.mode == "text_json":
            return AIMessage(content='{"summary": "库存风险中等", "enough": true}')
        if self.mode == "tools_fail":
            return AIMessage(content="SELECT 4 AS w LIMIT 1")
        return AIMessage(content="SELECT 4 AS w LIMIT 1")


# ----------------------------------------------------------------------
# A/B: generator OPT-05
# ----------------------------------------------------------------------

def test_generator_tool_calls():
    from app.tools.sql.generator import _llm_generate, repair_sql
    ctx = {"tables": [{"table": "inventory", "columns": "id, sku, stock_days"}], "metrics": [], "dictionary": ""}
    sql = _llm_generate("查库存", ctx, FakeModel("tool_calls"))
    assert sql == "SELECT 1 AS x LIMIT 1", f"A: {sql!r}"
    fixed = repair_sql("SELECT bad", "syntax error", ctx, FakeModel("repair_tool_calls"))
    assert fixed == "SELECT 2 AS y LIMIT 1", f"A: repair {fixed!r}"
    print("[A] tool_calls 通道提取 SQL（generate/repair）✅")


def test_generator_text_fallback():
    from app.tools.sql.generator import _llm_generate, repair_sql
    ctx = {"tables": [], "metrics": [], "dictionary": ""}
    sql = _llm_generate("查库存", ctx, FakeModel("text_sql"))
    assert sql == "SELECT 3 AS z LIMIT 1", f"B: {sql!r}"
    fixed = repair_sql("SELECT bad", "err", ctx, FakeModel("text_sql"))
    assert fixed == "SELECT 3 AS z LIMIT 1", f"B: repair {fixed!r}"
    # bind_tools 抛异常也降级
    sql2 = _llm_generate("查库存", ctx, FakeModel("tools_fail"))
    assert sql2 == "SELECT 4 AS w LIMIT 1", f"B: tools_fail {sql2!r}"
    print("[B] 文本降级（不支持 tool_calls / 抛异常）✅")


# ----------------------------------------------------------------------
# C/D: base._analyze OPT-04
# ----------------------------------------------------------------------

def _make_dept_agent(mode: str):
    import app.agents.base as base_mod

    base_mod.llm_available = lambda provider=None: True
    from app.agents.base import BaseDepartmentAgent

    class FakeDept(BaseDepartmentAgent):
        AGENT_NAME = "fake"
        KNOWN_REQS = frozenset()
        PRIORITY_TABLES = {}
        KEYWORD_MAP = {}
        FALLBACK_REQ = "inventory_risk"
        PLAN_PROMPT = ""
        ANALYSIS_PROMPT = "任务：{task}\n上下文：{context}\n结果：{result_json}"

        def _load_dictionary(self) -> str:
            return ""

    return FakeDept(model=FakeModel(mode), executor=lambda sql: {"columns": [], "rows": [], "row_count": 0})


def test_analyze_structured():
    ag = _make_dept_agent("structured_analysis")
    analysis, evidence, enough, missing = ag._analyze("库存风险", [{"requirement": "inventory_risk", "rows": []}])
    assert analysis[0] == "库存风险中等", f"C: {analysis!r}"
    assert any(e["type"] == "llm_metric" for e in evidence), "C: 缺 metric"
    assert any(e["type"] == "llm_anomaly" for e in evidence), "C: 缺 anomaly"
    assert enough is True and missing == []
    print("[C] _analyze 结构化通道 -> 同构 result（metrics/anomalies/evidence）✅")


def test_analyze_fallback():
    ag = _make_dept_agent("text_json")
    analysis, evidence, enough, missing = ag._analyze("库存风险", [{"requirement": "inventory_risk", "rows": []}])
    # 文本 JSON -> 旧 parse_analysis_json 路径
    assert analysis[0] == "库存风险中等", f"D: {analysis!r}"
    assert enough is True
    # 结构化失败（with_structured_output 抛异常）也应降级文本
    ag2 = _make_dept_agent("structured_fail")
    analysis2, evidence2, _, _ = ag2._analyze("库存风险", [{"requirement": "inventory_risk", "rows": []}])
    assert analysis2, f"D: fallback 空 {analysis2!r}"
    print("[D] _analyze 降级（文本 JSON 解析 / 结构化异常）✅")


# ----------------------------------------------------------------------
# E/F: decision.run OPT-04
# ----------------------------------------------------------------------

def _make_decision(mode: str):
    import app.agents.decision.agent as da_mod

    da_mod.llm_available = lambda provider=None: True
    from app.agents.decision.agent import DecisionAgent

    return DecisionAgent(model=FakeModel(mode))


def test_decision_structured():
    agent = _make_decision("structured_decision")
    report = agent.run("怎么处理库存", {"logistics": {"summary": "库存低", "confidence": 0.8}})
    assert report["summary"] == "建议补货", f"E: {report['summary']!r}"
    assert report["recommendations"][0]["priority"] == "P0"
    assert 0.0 <= report["confidence"] <= 1.0
    print("[E] decision 结构化通道 -> DecisionOutput dict ✅")


def test_decision_fallback():
    agent = _make_decision("text_json")
    report = agent.run("怎么处理库存", {"logistics": {"summary": "库存低"}})
    assert report["summary"] == "建议补货" or report["summary"], f"F: {report!r}"
    agent2 = _make_decision("structured_fail")
    report2 = agent2.run("怎么处理库存", {"logistics": {"summary": "库存低"}})
    assert report2["summary"], f"F: fallback 空 {report2!r}"
    print("[F] decision 降级（旧解析链 / 结构化异常）✅")


# ----------------------------------------------------------------------
# G: 真实 DeepSeek bind_tools 冒烟（可选）
# ----------------------------------------------------------------------

def test_live_bind_tools():
    from app.llm import llm_available

    if not llm_available():
        print("[G] 未配置真实 LLM key，跳过真实冒烟")
        return
    from app.tools.sql.generator import _invoke_sql

    sql = _invoke_sql(
        __import__("app.llm", fromlist=["get_chat_model"]).get_chat_model(tier="small"),
        "你是 SQL 专家。输出 SELECT 1。",
        "调用 generate_sql 工具输出 SQL：",
        tool_index=0,
    )
    assert "SELECT" in sql.upper(), f"G: 真实调用未返回 SQL: {sql!r}"
    print(f"[G] DeepSeek 真实 bind_tools 冒烟通过：{sql!r} ✅")


if __name__ == "__main__":
    test_generator_tool_calls()
    test_generator_text_fallback()
    test_analyze_structured()
    test_analyze_fallback()
    test_decision_structured()
    test_decision_fallback()
    test_live_bind_tools()
    print("\nOPT-04 + OPT-05 验证完成 ✅")
