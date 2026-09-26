# -*- coding: utf-8 -*-
"""统一封装（app/llm/structured.py）+ 全量 LLM 调用点结构化改造验证（2026-09-25）。

覆盖：
  A. structured.py 基础：extract_json / invoke_structured / invoke_tool / invoke_text
  B. base._plan：结构化成功 + 文本降级 + 白名单过滤
  C. manager.run：结构化成功（DAG 校验）+ 文本降级
  D. memory.extractor：结构化成功（写入统计）+ 文本降级
  E. memory.judge：结构化成功（normalize）+ 文本降级
  F. graph.quality：结构化 pass/fail + 文本降级
  G.（可选，需真实 key）DeepSeek 真实 bind_tools 冒烟

运行：.venv\\Scripts\\python -u scripts\\verify_structured_all.py
"""
import sys

sys.path.insert(0, ".")

from langchain_core.messages import AIMessage  # noqa: E402


# ----------------------------------------------------------------------
# FakeModel：mode=structured 用 payload 构造 schema 实例；text 返回 payload 文本
# ----------------------------------------------------------------------

class _FakeStructured:
    def __init__(self, model, schema):
        self._model = model
        self._schema = schema

    def invoke(self, messages):
        m = self._model
        if m.mode == "structured_fail":
            raise RuntimeError("structured output not supported")
        if m.mode == "structured":
            return self._schema(**m.payload)
        raise RuntimeError(f"unexpected mode: {m.mode}")


class FakeModel:
    def __init__(self, mode="structured", payload=None):
        self.mode = mode
        self.payload = payload or {}
        self.model_name = "fake-model"

    def bind_tools(self, tools):
        if self.mode == "tools_fail":
            raise RuntimeError("API does not support tools")
        return self

    def with_structured_output(self, schema, method="function_calling", **kwargs):
        return _FakeStructured(self, schema)

    def invoke(self, messages):
        if self.mode == "text":
            return AIMessage(content=str(self.payload))
        if self.mode == "tools_fail":
            return AIMessage(content="SELECT 1 LIMIT 1")
        raise RuntimeError(f"unexpected invoke mode: {self.mode}")


# ----------------------------------------------------------------------
# A: structured.py 基础
# ----------------------------------------------------------------------

def test_structured_base():
    from app.llm.structured import extract_json, invoke_structured, invoke_text, invoke_tool
    from pydantic import BaseModel, Field

    # extract_json
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}, "markdown 剥壳"
    assert extract_json('前缀 {"a": 1} 后缀') == {"a": 1}, "花括号截取"
    assert extract_json("纯文本") is None, "非法返回 None"
    assert extract_json('["list"]') is None, "非 dict 返回 None"

    class S(BaseModel):
        x: int = 0
        ys: list[str] = Field(default_factory=list)

    msgs = []
    # invoke_structured 成功
    d = invoke_structured(FakeModel("structured", {"x": 3, "ys": ["a"]}), S, msgs)
    assert d == {"x": 3, "ys": ["a"]}, f"A: {d!r}"
    # invoke_structured 失败
    assert invoke_structured(FakeModel("structured_fail"), S, msgs) is None, "异常 → None"
    # invoke_tool 成功 / 无 tool_calls / 异常
    tool = {"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}}}}
    class _TM(FakeModel):
        def bind_tools(self, tools):
            return self
        def invoke(self, messages):
            if self.mode == "tc":
                return AIMessage(content="", tool_calls=[{"name": "t", "args": {"sql": "SELECT 1"}, "id": "c"}])
            if self.mode == "plain":
                return AIMessage(content="SELECT 2")
            if self.mode == "boom":
                raise RuntimeError("net")
            raise RuntimeError(self.mode)
    val, text = invoke_tool(_TM("tc"), tool, msgs, "sql")
    assert (val, text) == ("SELECT 1", None), f"A: tc {(val, text)!r}"
    val, text = invoke_tool(_TM("plain"), tool, msgs, "sql")
    assert (val, text) == (None, "SELECT 2"), f"A: plain {(val, text)!r}"
    val, text = invoke_tool(_TM("boom"), tool, msgs, "sql")
    assert (val, text) == (None, None), f"A: boom {(val, text)!r}"
    # invoke_text
    assert invoke_text(FakeModel("text", "hi"), msgs) == "hi"
    assert invoke_text(_TM("boom"), msgs) is None
    print("[A] structured 基础（extract_json/invoke_structured/invoke_tool/invoke_text）✅")


# ----------------------------------------------------------------------
# B: base._plan
# ----------------------------------------------------------------------

def _make_dept_agent(mode, payload):
    import app.agents.base as base_mod

    base_mod.llm_available = lambda provider=None: True
    from app.agents.base import BaseDepartmentAgent

    class FakeDept(BaseDepartmentAgent):
        AGENT_NAME = "fake"
        KNOWN_REQS = frozenset({"inventory_risk", "delivery", "knowledge"})
        PRIORITY_TABLES = {}
        KEYWORD_MAP = {}
        FALLBACK_REQ = "inventory_risk"
        PLAN_PROMPT = "任务：{task}\n上下文：{context}"
        ANALYSIS_PROMPT = "任务：{task}\n上下文：{context}\n结果：{result_json}"

        def _load_dictionary(self) -> str:
            return ""

    return FakeDept(model=FakeModel(mode, payload), executor=lambda sql: {"rows": [], "row_count": 0})


def test_plan_structured():
    ag = _make_dept_agent("structured", {"data_domains": ["inventory_risk", "delivery", "unknown_domain"]})
    plan = ag._plan("查库存", {})
    assert plan == ["inventory_risk", "delivery"], f"B: {plan!r}（unknown 被白名单过滤）"
    print("[B] _plan 结构化通道 + 白名单过滤 ✅")


def test_plan_fallback():
    ag = _make_dept_agent("text", "- inventory_risk\n- delivery")
    plan = ag._plan("查库存", {})
    assert plan == ["inventory_risk", "delivery"], f"B: 文本降级 {plan!r}"
    # 结构化异常 → 文本 → 兜底 FALLBACK_REQ
    ag2 = _make_dept_agent("structured_fail", {})
    plan2 = ag2._plan("查库存", {})
    assert plan2 == ["inventory_risk"], f"B: 异常兜底 {plan2!r}"
    print("[B] _plan 文本降级 / 异常兜底 FALLBACK_REQ ✅")


# ----------------------------------------------------------------------
# C: manager.run
# ----------------------------------------------------------------------

def _make_manager(mode, payload):
    import app.agents.manager.agent as mgr

    mgr.llm_available = lambda provider=None: True
    from app.agents.manager.agent import ManagerAgent

    return ManagerAgent(model=FakeModel(mode, payload))


def test_manager_structured():
    agent = _make_manager("structured", {
        "intent": "sales_analysis",
        "tasks": [
            {"id": "op1", "agent": "operation", "depends_on": [], "description": "运营"},
            {"id": "bad", "agent": "hacker", "depends_on": [], "description": "非法"},
        ],
    })
    plan = agent.run("美国市场销售怎么样", memory=None)
    agents = [t["agent"] for t in plan["tasks"]]
    assert "hacker" not in agents, f"C: 非法 agent 未过滤 {agents}"
    assert "decision" in agents, f"C: decision 未补 {agents}"
    assert plan["required_agents"] == ["operation"], f"C: {plan['required_agents']!r}"
    assert plan["intent"] == "sales_analysis"
    print("[C] manager 结构化通道 + DAG 校验（过滤/补 decision）✅")


def test_manager_fallback():
    import json as _json
    agent = _make_manager("text", _json.dumps({
        "intent": "business_analysis",
        "tasks": [{"id": "op1", "agent": "operation", "depends_on": [], "description": "运营"}],
    }))
    plan = agent.run("查一下销售", memory=None)
    assert plan["required_agents"] == ["operation"], f"C: 文本降级 {plan!r}"
    print("[C] manager 文本降级（旧解析链）✅")


# ----------------------------------------------------------------------
# D: memory.extractor
# ----------------------------------------------------------------------

def test_extractor_structured():
    import app.memory.extractor as me

    me.llm_available = lambda provider=None: True
    me.upsert_profile = lambda *a, **k: None
    me.upsert_preference = lambda *a, **k: None
    me.add_memory = lambda *a, **k: None
    # extractor 是模块级 `from app.llm import get_chat_model`，monkeypatch 模块内引用
    _gcm_real = me.get_chat_model
    me.get_chat_model = lambda tier="medium": FakeModel("structured", {
        "profiles": [{"key": "market_scope", "value": "美国", "confidence": 0.9, "evidence": "我负责美国"}],
        "preferences": [{"key": "default_market", "value": "US", "evidence": "默认看美国"}],
        "memories": [{"type": "fact", "content": "Q3 目标 100w", "department": "operation", "confidence": 0.8, "evidence": "依据"}],
    })
    try:
        stats = me._extract_once("u1", "我负责美国市场", "好的")
        assert stats["profiles"] == 1 and stats["preferences"] == 1 and stats["memories"] == 1, f"D: {stats}"
        print("[D] extractor 结构化通道 + 写入统计 ✅")
    finally:
        me.get_chat_model = _gcm_real


def test_extractor_fallback():
    import app.memory.extractor as me

    me.llm_available = lambda provider=None: True
    me.upsert_profile = lambda *a, **k: None
    me.upsert_preference = lambda *a, **k: None
    me.add_memory = lambda *a, **k: None
    _gcm_real = me.get_chat_model
    me.get_chat_model = lambda tier="medium": FakeModel("text", '{"profiles": [{"key": "role", "value": "运营", "confidence": 0.95, "evidence": "我是运营"}]}')
    try:
        stats = me._extract_once("u1", "我是运营", "好的")
        assert stats["profiles"] == 1, f"D: 文本降级 {stats}"
        print("[D] extractor 文本降级 ✅")
    finally:
        me.get_chat_model = _gcm_real


# ----------------------------------------------------------------------
# E: memory.judge
# ----------------------------------------------------------------------

def test_judge_structured():
    import app.memory.judge as mj

    mj.llm_available = lambda provider=None: True
    _gcm_real = mj.get_chat_model
    mj.get_chat_model = lambda tier="small": FakeModel("structured", {
        "relation": "duplicate", "target_id": 3, "event": "NONE", "new_content": "", "reason": "同一件事",
    })
    try:
        out = mj.judge_memory("新记忆", "fact", [{"id": 3, "memory_type": "fact", "content": "旧", "confidence": 0.9}])
        assert out == {"relation": "duplicate", "target_id": 3, "event": "NONE", "new_content": "", "reason": "同一件事"}, f"E: {out!r}"
        print("[E] judge 结构化通道 + normalize ✅")
    finally:
        mj.get_chat_model = _gcm_real


def test_judge_fallback():
    import app.memory.judge as mj

    mj.llm_available = lambda provider=None: True
    _gcm_real = mj.get_chat_model
    mj.get_chat_model = lambda tier="small": FakeModel("text", '{"relation": "conflict", "target_id": 1, "event": "update", "new_content": "", "reason": "矛盾"}')
    try:
        out = mj.judge_memory("新", "preference", [{"id": 1, "memory_type": "preference", "content": "旧", "confidence": 0.8}])
        assert out["event"] == "UPDATE", f"E: 文本降级 normalize {out!r}"
        print("[E] judge 文本降级 + event 白名单规范化 ✅")
    finally:
        mj.get_chat_model = _gcm_real


# ----------------------------------------------------------------------
# F: graph.quality
# ----------------------------------------------------------------------

def test_quality_structured():
    import app.llm as llm_mod
    from app.graph.quality import llm_quality_check

    _gcm_real = llm_mod.get_chat_model
    llm_mod.get_chat_model = lambda tier="small": FakeModel("structured", {"passed": False, "issues": ["跑题", "漏答"]})
    try:
        issues = llm_quality_check("Q", "A")
        assert issues == ["跑题", "漏答"], f"F: {issues!r}"
        print("[F] quality_gate 结构化 fail ✅")
    finally:
        llm_mod.get_chat_model = _gcm_real

    llm_mod.get_chat_model = lambda tier="small": FakeModel("structured", {"passed": True})
    try:
        assert llm_quality_check("Q", "A") == []
        print("[F] quality_gate 结构化 pass ✅")
    finally:
        llm_mod.get_chat_model = _gcm_real


def test_quality_fallback():
    import app.llm as llm_mod
    from app.graph.quality import llm_quality_check

    _gcm_real = llm_mod.get_chat_model
    llm_mod.get_chat_model = lambda tier="small": FakeModel("text", '{"pass": 0, "issues": ["答非所问"]}')
    try:
        issues = llm_quality_check("Q", "A")
        assert issues == ["答非所问"], f"F: 文本降级 {issues!r}"
        print("[F] quality_gate 文本降级 ✅")
    finally:
        llm_mod.get_chat_model = _gcm_real


# ----------------------------------------------------------------------
# G: 真实 DeepSeek 冒烟（可选）
# ----------------------------------------------------------------------

def test_live():
    from app.llm import llm_available

    if not llm_available():
        print("[G] 未配置真实 LLM key，跳过")
        return
    from app.llm import get_chat_model
    from app.tools.sql.generator import _invoke_sql

    sql = _invoke_sql(
        get_chat_model(tier="small"),
        "你是 SQL 专家。输出 SELECT 1。",
        "调用 generate_sql 工具输出 SQL：",
        tool_index=0,
    )
    assert "SELECT" in sql.upper(), f"G: {sql!r}"
    print(f"[G] DeepSeek 真实 bind_tools 冒烟：{sql!r} ✅")


if __name__ == "__main__":
    test_structured_base()
    test_plan_structured()
    test_plan_fallback()
    test_manager_structured()
    test_manager_fallback()
    test_extractor_structured()
    test_extractor_fallback()
    test_judge_structured()
    test_judge_fallback()
    test_quality_structured()
    test_quality_fallback()
    test_live()
    print("\n统一封装 + 全量结构化改造验证完成 ✅")
