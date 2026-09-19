"""Product Strategy / Intelligence Agent：下一阶段做什么产品（设计文档 14 节）。

产品部门不是简单的"画产品 Agent"，而是行业研究、市场研究、竞品研究、消费者分析、
产品生命周期、开发计划的综合智能体，RAG 权重最高。
跨部门上下文（Operation/Finance/Logistics 结果）由 Manager 按依赖 DAG 注入。
"""

from app.agents.product.agent import ProductAgent, initial_state
from app.agents.product.graph import build_product_agent, run_product

__all__ = ["ProductAgent", "initial_state", "build_product_agent", "run_product"]
