"""路由调度：根据 task_plan DAG 调度部门 Agent（设计文档 6-7 节）。

采用"循环路由"模式：
  manager -> router -> [department subgraph] -> router -> ... -> decision -> END

router 节点每次找出下一个"就绪且可执行"的部门任务；
不可用的 Agent（尚未实现）标记为 skipped，其下游可继续执行；
所有部门任务完成/跳过后，路由到 decision。

Phase 1 仅 operation Agent 可用；finance/logistics/product 将在 Phase 3 接入。
"""

from __future__ import annotations

from typing import Any, Optional

from app.graph.planner import (
    all_departments_done,
    get_ready_tasks,
    task_by_id,
)
from app.observability.logging import get_logger

logger = get_logger("router")

# 已实现的部门 Agent -> 子图便捷入口（Phase 1 仅 operation）
# 新增部门 Agent 后，在此注册即可被 Router 调度。
AVAILABLE_AGENTS: dict[str, Any] = {}


def register_department(agent_name: str, runner: Any) -> None:
    """注册部门 Agent 的便捷运行入口（供 Router 调度）。"""
    AVAILABLE_AGENTS[agent_name] = runner


# 延迟导入 operation，避免循环依赖（operation 导入 graph.state）
def _get_operation_runner():
    from app.agents.operation import run_operation
    return run_operation


# ---------------------------------------------------------------------------
# 路由节点：找出下一个可执行任务
# ---------------------------------------------------------------------------

def router_node(state: dict[str, Any]) -> dict[str, Any]:
    """主 Graph 路由节点：找出下一个就绪且可用的部门任务。

    遍历就绪任务，跳过未实现的 Agent（记入 skipped_tasks），
    找到第一个可用任务则设为 current_task；全部部门完成则 current_task 为空（路由到 decision）。
    """
    task_plan = state.get("task_plan") or {}
    tasks = task_plan.get("tasks", [])
    completed = set(state.get("completed_tasks") or [])
    skipped = set(state.get("skipped_tasks") or [])

    # 确保 operation 已注册（延迟注册，避免模块加载时循环导入）
    if "operation" not in AVAILABLE_AGENTS:
        register_department("operation", _get_operation_runner())

    # 循环找可用任务：可能连续跳过多个未实现的 Agent
    guard = 0
    while guard < len(tasks) + 1:
        guard += 1
        ready = get_ready_tasks(tasks, completed, skipped)
        dept_ready = [t for t in ready if t.get("agent") != "decision"]
        if not dept_ready:
            break

        found_available = False
        for task in dept_ready:
            agent = task.get("agent", "")
            if agent in AVAILABLE_AGENTS:
                logger.info("router.dispatch", task_id=task["id"], agent=agent)
                return {
                    "current_task": task["id"],
                    "skipped_tasks": list(skipped),
                    "completed_tasks": list(completed),
                }
            else:
                # Agent 未实现，标记跳过（其下游依赖可继续）
                logger.warning("router.skip_unavailable", task_id=task["id"], agent=agent)
                skipped.add(task["id"])
                found_available = True

        if not found_available:
            break

    # 没有可执行的部门任务了
    if all_departments_done(tasks, completed, skipped):
        logger.info("router.all_departments_done", completed=len(completed), skipped=len(skipped))
    return {
        "current_task": "",
        "skipped_tasks": list(skipped),
        "completed_tasks": list(completed),
    }


def route_fn(state: dict[str, Any]) -> str:
    """条件边函数：根据 current_task 决定下一个节点。

    Returns:
        部门 agent 名（"operation" / "finance" / ...）或 "decision"。
    """
    current = state.get("current_task", "")
    if not current:
        return "decision"
    task_plan = state.get("task_plan") or {}
    task = task_by_id(task_plan.get("tasks", []), current)
    if task:
        return task.get("agent", "decision")
    return "decision"


# ---------------------------------------------------------------------------
# 部门节点包装器：调用子图并回写结果
# ---------------------------------------------------------------------------

def make_department_node(agent_name: str):
    """工厂函数：为指定部门 Agent 创建主 Graph 节点函数。

    节点职责：
    1. 从 task_plan 取当前任务的描述；
    2. 调用部门子图便捷入口；
    3. 结果写入 department_results[agent_name]；
    4. 任务 id 加入 completed_tasks，current_task 清空。
    """
    def node(state: dict[str, Any]) -> dict[str, Any]:
        task_plan = state.get("task_plan") or {}
        tasks = task_plan.get("tasks", [])
        current = state.get("current_task", "")
        task = task_by_id(tasks, current)

        # 任务描述优先，回退到用户问题
        task_desc = (task or {}).get("description") or state.get("user_question", "")
        user_question = state.get("user_question", "")

        logger.info("department.node.start", agent=agent_name, task_id=current, task=task_desc[:80])

        try:
            runner = AVAILABLE_AGENTS.get(agent_name)
            if runner is None:
                raise RuntimeError(f"Agent {agent_name} 未注册")
            result = runner(task_desc)
        except Exception as exc:
            logger.error("department.node.fail", agent=agent_name, error=str(exc))
            result = {
                "agent": agent_name,
                "task": task_desc,
                "summary": f"{agent_name} Agent 执行失败: {exc}",
                "error": str(exc),
                "confidence": 0.0,
            }

        # 回写结果与执行状态
        department_results = dict(state.get("department_results") or {})
        department_results[agent_name] = result

        completed = list(state.get("completed_tasks") or [])
        if current and current not in completed:
            completed.append(current)

        logger.info("department.node.done", agent=agent_name, task_id=current, confidence=result.get("confidence"))
        return {
            "department_results": department_results,
            "completed_tasks": completed,
            "current_task": "",
        }

    return node
