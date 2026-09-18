"""任务理解与 Planner（设计文档 4、6 节）。

用户问题 -> 理解 -> 目标 -> 涉及部门 -> 任务拆解 -> 依赖 DAG（depends_on）。

TODO(Phase 4): LLM 输出结构化 task_plan：
```yaml
tasks:
  - id: operation_analysis
    agent: operation
    depends_on: []
  - id: product_strategy
    agent: product
    depends_on: [operation_analysis, finance_analysis, logistics_analysis]
```
"""
