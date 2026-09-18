"""State 分层设计（设计文档 8-10 节）。

GlobalState 只保存跨部门必须共享的最小信息（公司会议室）；
部门级 State 各自独立（各部门工作台），仅向 GlobalState 回传最终结果、关键证据、关键指标、置信度。
"""

from typing import Any, TypedDict


class GlobalState(TypedDict, total=False):
    """全局共享 State。"""

    # 会话标识
    thread_id: str
    user_id: str

    # 用户问题
    user_question: str

    # 规划结果
    task_plan: dict[str, Any]
    required_agents: list[str]

    # 当前执行阶段（planning / running / interrupted / done / error）
    current_stage: str

    # 各部门最终结果：{agent_name: department_result}
    department_results: dict[str, Any]

    # 执行追踪（主 Graph 调度用，设计文档 6 节 DAG 执行）
    completed_tasks: list[str]       # 已完成任务 id 列表
    skipped_tasks: list[str]         # 因 Agent 未实现而跳过的任务 id
    current_task: str                # 当前正在执行的任务 id

    # Product Agent 跨部门上下文（由 Manager 按依赖 DAG 注入）
    product_context: dict[str, Any]

    # Decision Agent 最终结论
    decision_result: dict[str, Any]

    # Human-in-the-loop 中断载荷
    interrupt_payload: dict[str, Any]

    # 错误状态
    error_state: dict[str, Any]

    # 最终答案
    final_answer: str


class DepartmentState(TypedDict, total=False):
    """部门 Agent 通用内部 State（各部门在此工作台内自由循环）。"""

    task: str
    messages: list[dict[str, Any]]
    iteration: int
    tool_calls: list[dict[str, Any]]
    sql_history: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    analysis: list[str]
    evidence: list[dict[str, Any]]
    final_result: dict[str, Any]
    error: dict[str, Any]

    # 重试计数（设计文档 42 节）
    sql_retry_count: int
    tool_retry_count: int
    max_iterations: int
