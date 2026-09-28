"""State 分层设计（设计文档 8-10 节）。

GlobalState 只保存跨部门必须共享的最小信息（公司会议室）；
部门级 State 各自独立（各部门工作台），仅向 GlobalState 回传最终结果、关键证据、关键指标、置信度。
"""

from typing import Annotated, Any, TypedDict


def _add_unique(left: list, right: list) -> list:
    """reducer：列表去重合并（并行节点写 completed/skipped_tasks 不互相覆盖）。"""
    merged = list(left or [])
    for x in right or []:
        if x not in merged:
            merged.append(x)
    return merged


def _merge_dict(left: dict, right: dict) -> dict:
    """reducer：dict 合并（并行节点写 department_results 不同 key 不互相覆盖）。"""
    out = dict(left or {})
    out.update(right or {})
    return out


class GlobalState(TypedDict, total=False):
    """全局共享 State。"""

    # 会话标识
    thread_id: str
    user_id: str

    # 用户问题
    user_question: str

    # 安全（OPT-06）：入口注入检测命中后注入各 Agent 的边界警告文本（空串=未命中）
    injection_warning: str

    # 多轮会话（OPT-12）：
    # conversation_context = 历史摘要 + 最近 N 轮原文（注入 Manager/Decision，解决多轮指代）
    # rewritten_question   = 结合历史改写后的自包含问题（首轮=原文；规划/汇总以它为准）
    conversation_context: str
    rewritten_question: str

    # 规划结果
    task_plan: dict[str, Any]
    required_agents: list[str]

    # 当前执行阶段（planning / running / interrupted / done / error）
    current_stage: str

    # 各部门最终结果：{agent_name: department_result}
    # Annotated reducer：Operation/Finance/Logistics 并行写各自 key，合并不覆盖
    department_results: Annotated[dict[str, Any], _merge_dict]

    # 执行追踪（主 Graph 调度用，设计文档 6 节 DAG 执行）
    completed_tasks: Annotated[list[str], _add_unique]   # 已完成任务 id 列表
    skipped_tasks: Annotated[list[str], _add_unique]     # 因 Agent 未实现而跳过的任务 id，skipped_tasks 就是把 "规划与实现的差集" 显式记录下来的agent任务
    current_task: str                                    # 遗留字段（并行路由后不再使用，保留兼容）

    # Product Agent 跨部门上下文（由 Manager 按依赖 DAG 注入）
    product_context: dict[str, Any]

    # Decision Agent 最终结论
    decision_result: dict[str, Any]

    # 质量自纠回路（考点二十九）：quality_gate 评估 decision 输出，
    # 不合格则带 feedback 回 decision 重生成；自动重试耗尽后 interrupt() 等用户纠正
    quality_iteration: int          # 已回炉重生成次数（自动 + 用户反馈轮）
    quality_feedback: str           # 偏差诊断/用户纠正意见（传给 decision 重生成的修正指令）
    quality_check: dict[str, Any]   # 质量门评估结果（pass / issues / source / approved / force_pass）

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
