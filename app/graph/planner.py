"""任务 DAG 工具（设计文档 6 节）。

提供任务依赖图的拓扑排序、就绪任务查询、完成度判断等纯函数。
Manager 输出 task_plan 后，主 Graph 的 Router 用这些函数决定下一步调度哪个 Agent。
"""

from __future__ import annotations

from typing import Any, Optional


def topological_sort(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对任务列表按依赖关系做拓扑排序（Kahn 算法）。

    若存在循环依赖，返回原顺序（调用方应已做环检测）。
    """
    task_map = {t["id"]: t for t in tasks}
    in_degree: dict[str, int] = {t["id"]: 0 for t in tasks}
    dependents: dict[str, list[str]] = {t["id"]: [] for t in tasks}

    for t in tasks:
        for dep in t.get("depends_on", []):
            if dep in task_map:
                in_degree[t["id"]] += 1
                dependents[dep].append(t["id"])

    queue = [tid for tid, deg in in_degree.items() if deg == 0]
    sorted_ids: list[str] = []
    while queue:
        tid = queue.pop(0)
        sorted_ids.append(tid)
        for dep_tid in dependents[tid]:
            in_degree[dep_tid] -= 1
            if in_degree[dep_tid] == 0:
                queue.append(dep_tid)

    if len(sorted_ids) != len(tasks):
        # 有环，降级返回原顺序
        return list(tasks)
    return [task_map[tid] for tid in sorted_ids]

# "tasks": [
#   {"id": "operation_analysis", "agent": "operation", "depends_on": []},
#   {"id": "finance_analysis",   "agent": "finance",   "depends_on": []},
#   {"id": "logistics_analysis", "agent": "logistics", "depends_on": []},
#   {"id": "product_analysis",   "agent": "product",
#    "depends_on": ["operation_analysis", "finance_analysis", "logistics_analysis"]},
#   {"id": "decision_analysis",  "agent": "decision",
#    "depends_on": ["operation_analysis", "finance_analysis", "logistics_analysis", "product_analysis"]}
# ]
def get_ready_tasks(
    tasks: list[dict[str, Any]],
    completed_ids: set[str],
    skipped_ids: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """ 找"依赖全完成且自己没做"的任务
    返回所有"就绪"任务：依赖全部完成（或被跳过）且自身未完成/未跳过。

    被跳过的任务视为已完成（其下游可以继续执行）。
    """
    skipped_ids = skipped_ids or set()
    done = completed_ids | skipped_ids
    ready: list[dict[str, Any]] = []
    for t in tasks:
        if t["id"] in completed_ids or t["id"] in skipped_ids:
            continue
        deps = t.get("depends_on", [])
        # all([True, True])  # True   —— 全是真
        # all([True, False])  # False  —— 有一个假
        # all([])  # True   —— 里面一个元素都没有
        #第一轮 deps = []   all(d in done for d in [])  → all(空序列) = True
        if all(d in done for d in deps):
            #`get_ready_tasks` 现在返回 `{P任务}`（O/F/L 在 completed 里，P 的依赖全满足）
            ready.append(t)
    return ready


def get_department_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回所有部门任务（排除 decision）。"""
    return [t for t in tasks if t.get("agent") != "decision"]


def get_decision_task(tasks: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """返回 decision 任务（若存在）。"""
    for t in tasks:
        if t.get("agent") == "decision":
            return t
    return None


def all_departments_done(
    tasks: list[dict[str, Any]],
    completed_ids: set[str],
    skipped_ids: Optional[set[str]] = None,
) -> bool:
    """判断所有部门任务是否都已完成或跳过。"""
    skipped_ids = skipped_ids or set()
    dept = get_department_tasks(tasks)
    if not dept:
        return True
    return all(t["id"] in completed_ids or t["id"] in skipped_ids for t in dept)


def task_by_id(tasks: list[dict[str, Any]], task_id: str) -> Optional[dict[str, Any]]:
    """按 id 查找任务。"""
    for t in tasks:
        if t["id"] == task_id:
            return t
    return None
