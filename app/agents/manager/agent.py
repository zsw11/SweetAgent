"""Manager Agent 主逻辑（设计文档 4、6 节）。

负责"做事之前"：理解问题 -> 确定目标 -> 判断涉及部门 -> 拆解任务 -> 建立依赖 DAG。
不做数据分析、不查数据库、不出业务结论。

输出 task_plan（含 tasks 列表与 depends_on 依赖关系），供主 Graph 的 Router 调度。
"""

from __future__ import annotations

from typing import Any, Optional, Union

from pydantic import BaseModel, Field

from app.agents.manager.prompts import MANAGER_PLAN_PROMPT, MANAGER_SYSTEM_PROMPT
from app.agents.manager.state import ManagerState
from app.config.settings import settings
from app.llm import get_chat_model, llm_available
from app.llm.structured import extract_json, invoke_structured, invoke_text
from app.observability.logging import get_logger

logger = get_logger("manager_agent")

# 已知部门 Agent（与设计文档 3.1 节一致）
_KNOWN_DEPARTMENTS: frozenset[str] = frozenset({"operation", "finance", "logistics", "product"})


# ---------------------------------------------------------------------------
# OPT-04 结构化输出：任务 DAG schema（with_structured_output 用）
# ---------------------------------------------------------------------------

class ManagerTaskItem(BaseModel):
    id: str = ""
    agent: str = ""
    depends_on: list[str] = Field(default_factory=list)
    description: str = ""


class ManagerPlanOutput(BaseModel):
    """Manager 规划输出 schema（与 MANAGER_PLAN_PROMPT 要求 JSON 同构）。"""
    intent: str = "business_analysis"
    tasks: list[ManagerTaskItem] = Field(default_factory=list)


class ManagerAgent:
    """Manager Agent：问题理解、任务拆解、依赖 DAG 规划（纯 LLM 规划，不查数据）。"""

    def __init__(self, model=None):
        if not llm_available():
            raise RuntimeError(
                "未配置 LLM API Key。Manager Agent 需要 LLM 做任务规划，"
                "请在 .env 中配置 Key 后重试。"
            )
        # Manager 需要强推理（设计文档 55 节：Manager → 强推理模型）
        self.model = model if model is not None else get_chat_model(tier="strong")

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def run(
        self,
        user_question: str,
        memory: Optional[str] = None,
        injection_warning: str = "",
        conversation_context: str = "",
    ) -> dict[str, Any]:
        """执行一次任务规划，返回 task_plan（含 tasks / required_agents / intent）。

        memory: 用户长期记忆文本（画像/偏好/通用记忆，注入规划 prompt）。
        injection_warning: 入口注入检测命中的边界警告（OPT-06），非空时作为
            独立 System 消息注入，要求忽略用户输入中的指令性内容。
        conversation_context: 多轮会话上下文（OPT-12：历史摘要 + 最近对话），
            非空时注入规划 prompt，使本轮规划承接前几轮主题。
        """
        logger.info("manager.run.start", question=user_question[:100])
        result = self._plan(
            user_question,
            memory=memory,
            injection_warning=injection_warning,
            conversation_context=conversation_context,
        )
        if isinstance(result, dict):
            # 结构化通道：仍须过公共 DAG 校验（过滤未知 agent、补 decision、环检测）
            plan = self._validate_plan(result, user_question)
        else:
            plan = self._parse_and_validate(result, user_question)
        logger.info(
            "manager.run.done",
            intent=plan.get("intent"),
            required_agents=plan.get("required_agents"),
            task_count=len(plan.get("tasks", [])),
        )
        return plan

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------
    def _plan(
        self,
        user_question: str,
        memory: Optional[str] = None,
        injection_warning: str = "",
        conversation_context: str = "",
    ) -> Union[dict[str, Any], str]:
        """生成任务 DAG：结构化通道（OPT-04）优先返回 dict；失败降级原始文本。"""
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [
            SystemMessage(content=MANAGER_SYSTEM_PROMPT),
            HumanMessage(content=MANAGER_PLAN_PROMPT.format(
                user_question=user_question,
                memory=memory or "（无）",
                conversation_context=conversation_context or "（无）",
            )),
        ]
        # OPT-06：注入检测命中时追加边界警告（系统级约束，独立消息不被用户输入稀释）
        if injection_warning:
            messages.append(SystemMessage(content=injection_warning))
        d = invoke_structured(self.model, ManagerPlanOutput, messages, logger_name="manager_agent")
        if d is not None:
            logger.info("manager.plan.structured.ok", task_count=len(d.get("tasks") or []))
            return d
        raw = invoke_text(self.model, messages, logger_name="manager_agent") or ""
        logger.debug("manager.plan.llm", raw=raw[:2000])
        return raw

    def _parse_and_validate(self, raw: str, user_question: str) -> dict[str, Any]:
        """文本降级路径：解析 LLM 输出 JSON 后走公共 DAG 校验。"""
        parsed = _parse_plan_json(raw)
        if not parsed or not isinstance(parsed.get("tasks"), list):
            logger.warning("manager.parse.failed", raw_preview=raw[:500])
            return _fallback_plan(user_question)
        return self._validate_plan(parsed, user_question)

    def _validate_plan(self, parsed: dict[str, Any], user_question: str) -> dict[str, Any]:
        """公共 DAG 校验（结构化 dict 与文本解析共用）：过滤未知 agent、补 decision、环检测。"""
        tasks = parsed["tasks"]
        # 1) 过滤：只保留已知部门 + decision；agent 字段缺失的跳过
        valid_tasks: list[dict[str, Any]] = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            agent = str(t.get("agent", "")).strip().lower()
            if agent not in _KNOWN_DEPARTMENTS and agent != "decision":
                logger.warning("manager.plan.unknown_agent", agent=agent, task_id=t.get("id"))
                continue
            valid_tasks.append({
                "id": str(t.get("id", f"{agent}_analysis")),
                "agent": agent,
                "depends_on": [str(d) for d in (t.get("depends_on") or [])],
                "description": str(t.get("description", "")),
            })

        # 2) 提取部门任务（非 decision）,没有任务给个默认任务
        dept_tasks = [t for t in valid_tasks if t["agent"] in _KNOWN_DEPARTMENTS]
        if not dept_tasks:
            logger.warning("manager.plan.no_department", fallback_to="operation")
            dept_tasks = [{"id": "operation_analysis", "agent": "operation", "depends_on": [], "description": "运营数据分析"}]
            valid_tasks = list(dept_tasks)

        # 3) 校验依赖：depends_on 必须指向存在的任务 id；不存在的依赖移除
        task_ids = {t["id"] for t in valid_tasks}
        for t in valid_tasks:
            t["depends_on"] = [d for d in t["depends_on"] if d in task_ids]

        # 4) product 必须依赖所有已选的 operation/finance/logistics（设计文档 6 节）
        upstream_ids = [t["id"] for t in dept_tasks if t["agent"] in ("operation", "finance", "logistics")]
        for t in valid_tasks:
            if t["agent"] == "product":
                for uid in upstream_ids:
                    if uid not in t["depends_on"]:
                        t["depends_on"].append(uid)

        # 5) 确保 decision 任务存在且依赖所有部门任务
        dept_ids = [t["id"] for t in dept_tasks]
        decision_tasks = [t for t in valid_tasks if t["agent"] == "decision"]
        if decision_tasks:
            decision_tasks[0]["depends_on"] = list(dept_ids)
            decision_tasks[0]["description"] = decision_tasks[0].get("description") or "跨部门结果汇总与决策建议"
        else:
            valid_tasks.append({
                "id": "decision",
                "agent": "decision",
                "depends_on": list(dept_ids),
                "description": "跨部门结果汇总与决策建议",
            })

        # 6) 环检测：DAG 不能有循环依赖
        if _has_cycle(valid_tasks):
            logger.error("manager.plan.cycle_detected", tasks=valid_tasks)
            # 降级：打破所有非 decision 的依赖，确保可执行
            for t in valid_tasks:
                if t["agent"] != "decision":
                    t["depends_on"] = []
            # 重新设置 decision 依赖
            for t in valid_tasks:
                if t["agent"] == "decision":
                    t["depends_on"] = list(dept_ids)

        required_agents = [t["agent"] for t in dept_tasks]
        return {
            "intent": str(parsed.get("intent", "")).strip() or "business_analysis",
            "required_agents": required_agents,
            "tasks": valid_tasks,
        }


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _parse_plan_json(text: str) -> Optional[dict[str, Any]]:
    """从 LLM 输出中提取 JSON（统一委托 extract_json：markdown 剥壳 + 花括号截取）。"""
    return extract_json(text)


def _has_cycle(tasks: list[dict[str, Any]]) -> bool:
    """检测任务 DAG 是否有循环依赖（DFS 三色标记）。"""
    graph = {t["id"]: t.get("depends_on", []) for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in graph}

    def dfs(node: str) -> bool:
        color[node] = GRAY
        for dep in graph.get(node, []):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                return True
            if color[dep] == WHITE and dfs(dep):
                return True
        color[node] = BLACK
        return False

    for tid in graph:
        if color[tid] == WHITE and dfs(tid):
            return True
    return False


def _fallback_plan(user_question: str) -> dict[str, Any]:
    """LLM 输出无法解析时的降级计划：单部门 operation + decision。"""
    return {
        "intent": "business_analysis",
        "required_agents": ["operation"],
        "tasks": [
            {"id": "operation_analysis", "agent": "operation", "depends_on": [], "description": "运营数据分析"},
            {"id": "decision", "agent": "decision", "depends_on": ["operation_analysis"], "description": "跨部门结果汇总与决策建议"},
        ],
    }


# 供主 Graph 复用的 state 构建辅助
def initial_state(user_question: str) -> ManagerState:
    """构造 ManagerState 初始值。"""
    return {
        "user_question": user_question,
        "intent": "",
        "task_plan": {},
        "required_agents": [],
        "missing_info": [],
        "planning_errors": [],
    }
