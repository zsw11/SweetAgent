"""路由：根据 task_plan 调度部门 Agent，并处理依赖（设计文档 6-7 节）。

并行执行无依赖任务；有依赖（如 Product 依赖 O/F/L）时先等前置结果再执行。

TODO(Phase 4): 基于 task_plan 实现依赖调度与上下文注入。
"""
