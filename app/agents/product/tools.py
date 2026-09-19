"""Product Agent 工具注册（设计文档 14 节）。

复用 Operation 的 SQL Tool System；知识库检索当前以确定性 SQL 过滤实现
（knowledge_chunks 按 department / 关键词过滤，种子向量为随机值勿用相似度），
RAG 向量检索留待 app/knowledge 模块接入（设计文档 35-38 节）。
"""

from app.agents.operation.tools import get_operation_tool_map


def get_product_tools():
    """返回 Product Agent 可用的工具列表（与 Operation 共享同一套 SQL 工具）。"""
    from app.agents.operation.tools import get_operation_tools
    return get_operation_tools()


def get_product_tool_map():
    """返回 {工具名: 可调用对象}，供 ProductAgent 内部按名调用。"""
    return get_operation_tool_map()
