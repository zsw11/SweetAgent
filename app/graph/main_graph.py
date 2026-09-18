"""主图：Parent Graph + Department SubGraph（设计文档 7 节）。

START -> manager -> planner -> route -> department subgraphs -> dependency check -> decision -> END。

TODO(Phase 1-5): 按阶段逐步实现。
"""

from __future__ import annotations


def build_main_graph():
    """构建 LangGraph 主图。

    TODO(Phase 1): 先跑通 Manager -> Operation -> Decision 最小闭环（文档 58 节）。
    """
    raise NotImplementedError("主图将在 Phase 1-5 逐步实现")


def run_question(question: str, thread_id: str, user_id: str):
    """执行一次用户问题（入口）。

    TODO(Phase 1): 调用 build_main_graph().invoke(...)。
    """
    raise NotImplementedError("Phase 1 实现")
