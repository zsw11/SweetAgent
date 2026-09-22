"""路由调度：根据 task_plan DAG 调度部门 Agent（设计文档 6-7 节）。

并行循环路由模式（2026-09-19 优化）：
  manager -> router -┳-> operation ━┓
                     ┣-> finance  ━┫-> router -> product -> router -> decision -> END
                     ┗-> logistics━┛

- router 条件边返回"就绪 agent 列表"，LangGraph 并行 fan-out 无依赖部门
  （Operation / Finance / Logistics 互不依赖，并行执行）；
- 各部门完成后 fan-in 回 router（等待本批全部完成）；
- Product 依赖 O/F/L，只在 O/F/L 全部完成后才就绪（串行阶段，由 DAG 依赖天然保证）；
- 不可用的 Agent（尚未实现）由 router 标记 skipped，其下游可继续执行。
"""

from __future__ import annotations

from typing import Any

from app.graph.planner import get_ready_tasks
from app.observability.logging import get_logger

logger = get_logger("router")

# 已实现的所有部门 Agent -> 子图便捷入口（O/F/L/P 均已接入）
# 新增部门 Agent 后，在此注册即可被 Router 调度。
AVAILABLE_AGENTS: dict[str, Any] = {}


def register_department(agent_name: str, runner: Any) -> None:
    """注册部门 Agent 的便捷运行入口（供 Router 调度）。"""
    AVAILABLE_AGENTS[agent_name] = runner


# 延迟导入 operation / finance / logistics / product，避免循环依赖, 执行具体的子agent
def _get_operation_runner():
    from app.agents.operation import run_operation
    return run_operation


def _get_finance_runner():
    from app.agents.finance import run_finance
    return run_finance


def _get_logistics_runner():
    from app.agents.logistics import run_logistics
    return run_logistics


def _get_product_runner():
    from app.agents.product import run_product
    return run_product


def _ensure_registered() -> None:
    """确保所有部门 Agent 已注册（延迟注册，避免模块加载时循环导入）。"""
    for name, getter in [
        ("operation", _get_operation_runner),
        ("finance", _get_finance_runner),
        ("logistics", _get_logistics_runner),
        ("product", _get_product_runner),
    ]:
        if name not in AVAILABLE_AGENTS:
            register_department(name, getter())


# ---------------------------------------------------------------------------
# 路由节点：跳过未实现 Agent（副作用）
# ---------------------------------------------------------------------------

def router_node(state: dict[str, Any]) -> dict[str, Any]:
    """主 Graph 路由节点：把"就绪但 Agent 未实现"的任务全部标记 skipped。

    跳过可能解锁下游依赖，因此循环直到稳定；可用的就绪任务由 route_fn 决策
    （并行 fan-out），本节点只负责副作用，不写 current_task。
    """
    task_plan = state.get("task_plan") or {}
    tasks = task_plan.get("tasks", [])
    completed = set(state.get("completed_tasks") or [])
    skipped = set(state.get("skipped_tasks") or [])

    _ensure_registered()

    changed = True
    while changed:
        changed = False
        ready = get_ready_tasks(tasks, completed, skipped)
        for t in ready:
            agent = t.get("agent", "")
            if agent == "decision":
                continue
            if agent not in AVAILABLE_AGENTS and t["id"] not in skipped:
                logger.warning("router.skip_unavailable", task_id=t["id"], agent=agent)
                skipped.add(t["id"])
                changed = True

    return {"skipped_tasks": list(skipped)}


def route_fn(state: dict[str, Any]) -> list[str]:
    """条件边函数：返回本批"就绪且可用"的部门 agent 列表（并行 fan-out）。

    Returns:
        就绪部门 agent 名列表（如 ["operation", "finance", "logistics"]）；
        全部部门完成/跳过后返回 ["decision"]。
        Product 依赖 O/F/L，只会在 O/F/L 全部完成后的批次出现（串行阶段）。
    """
    _ensure_registered()
    task_plan = state.get("task_plan") or {}
    tasks = task_plan.get("tasks", [])
    completed = set(state.get("completed_tasks") or [])
    skipped = set(state.get("skipped_tasks") or [])

    agents: list[str] = []
    ready = get_ready_tasks(tasks, completed, skipped)
    for t in ready:
        agent = t.get("agent", "")
        if agent == "decision":
            continue
        if agent in AVAILABLE_AGENTS and agent not in agents:
            agents.append(agent)

    if agents:
        return agents
    return ["decision"]


# ---------------------------------------------------------------------------
# 部门节点包装器：调用子图并回写结果
# ---------------------------------------------------------------------------

def make_department_node(agent_name: str, context_builder=None):
    """工厂函数：为指定部门 Agent 创建主 Graph 节点函数（并行安全）。

    节点职责：
    1. 从 task_plan 找"属于本部门、未完成未跳过"的第一个任务（自定位，不依赖 current_task）；
    2. 调用部门子图便捷入口（runner(task_desc, context=...)）；
    3. 返回增量更新（department_results 单 key / completed_tasks 单 id），
       由 GlobalState 的 Annotated reducer 去重合并——并行节点互不覆盖；
    4. 无任务可执行时返回 {}（幂等保护，防重复 fan-out 时重复执行）。

    Args:
        agent_name: 部门名（"operation"/"finance"/"logistics"/"product"）。
        context_builder: 可选函数 state -> context dict，用于注入跨部门上下文
            （如 Product 节点注入 Operation/Finance/Logistics 结果，设计文档 6 节）。
    """
    def node(state: dict[str, Any]) -> dict[str, Any]:
        task_plan = state.get("task_plan") or {}
        tasks = task_plan.get("tasks", [])
        completed = set(state.get("completed_tasks") or [])
        skipped = set(state.get("skipped_tasks") or [])

        # 自定位：属于本部门、未完成未跳过的第一个任务
        task = None
        for t in tasks:
            if t.get("agent") == agent_name and t["id"] not in completed and t["id"] not in skipped:
                task = t
                break
        if task is None:
            logger.warning("department.node.noop", agent=agent_name)
            return {}

        task_id = task["id"]
        task_desc = task.get("description") or state.get("user_question", "")

        logger.info("department.node.start", agent=agent_name, task_id=task_id, task=task_desc[:80])

        try:
            runner = AVAILABLE_AGENTS.get(agent_name)
            if runner is None:
                raise RuntimeError(f"Agent {agent_name} 未注册")
            context = context_builder(state) if context_builder else None
            result = runner(task_desc, context=context)
        except Exception as exc:
            logger.error("department.node.fail", agent=agent_name, error=str(exc))
            result = {
                "agent": agent_name,
                "task": task_desc,
                "summary": f"{agent_name} Agent 执行失败: {exc}",
                "error": str(exc),
                "confidence": 0.0,
            }

        logger.info("department.node.done", agent=agent_name, task_id=task_id, confidence=result.get("confidence"))
        return {
            "department_results": {agent_name: result},
            "completed_tasks": [task_id],
        }

    return node
