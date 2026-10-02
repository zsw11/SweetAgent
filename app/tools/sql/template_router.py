"""模板 SQL 路由（预留通道，未实现 —— 考点五十八/五十九设计稿）。

分层混合路由：模板兜高频固定口径（零成本、确定性）→ LLM 兜开放问答 →
成功 SQL 固化回流模板库形成正循环；两条通道共用同一道安全闸
（sqlglot 校验 + 数据域白名单 + 只读执行器）。

当前状态：仅预留接口与数据结构，所有函数返回 None / 空实现，
行为与"无模板层"完全一致。实现计划见各函数 TODO。

设计要点（与开发日志考点五十八/五十九一致）：
1. 路由判定不做"硬分类"：给置信度，命中且置信 ≥ 阈值才走模板通道，
   miss/低置信走 LLM 通道；0.5~0.8 区间可双跑交叉验证。
2. 回流要设"可固化判定"闸门：不是所有成功 SQL 都值得固化。
3. 去参数化先规则后 LLM：日期/数字/引号字符串用正则，LLM 只处理复杂槽位。
4. 安全闸统一在出口：模板省的是生成成本，不是安全成本。
5. 语义层（指标口径注册表）等口径冲突真实出现后再引入，此处不预建。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.config.settings import settings

# 将来存放模板的存储：可先用 JSON/YAML 文件，量大后迁移到 sql_templates 表
# TODO(模板路由): 定义模板存储与加载（预置模板 + 回流模板）
_TEMPLATE_STORE: dict[str, Any] = {}


@dataclass
class SqlTemplate:
    """一个参数化 SQL 模板。"""
    template_id: str
    intent: str                    # 意图描述（路由匹配用）
    sql: str                       # 参数化 SQL，槽位形如 {start_date} {brand}
    params: dict[str, Any] = field(default_factory=dict)  # 槽位 schema：名称 → 类型(日期/枚举/数值)
    department: str = ""           # 部门白名单（防跨部门模板误用）
    description: str = ""          # 口径说明（审计/语义层信号）
    hit_count: int = 0             # 命中统计（回流判定依据之一）


@dataclass
class TemplateMatch:
    """路由判定命中结果。"""
    template: SqlTemplate
    confidence: float              # 匹配置信度（与 SQL_TEMPLATE_CONFIDENCE_THRESHOLD 比较）
    filled_sql: str                # 参数填充后的可执行 SQL（仍需过统一安全闸）


def match_template(task: str, context: Optional[dict[str, Any]] = None) -> Optional[TemplateMatch]:
    """路由判定：任务是否命中模板库。

    未命中 / 置信不足返回 None，由调用方继续走 LLM 生成链。

    TODO(模板路由):
      1. 模板匹配器：意图分类 / 关键词 / 向量召回模板库
      2. 置信度计算与阈值比较（settings.SQL_TEMPLATE_CONFIDENCE_THRESHOLD）
      3. 参数槽位填充 + 槽位类型校验（日期/枚举/数值）
      4. 填充后的 SQL 仍走统一安全闸（validate_and_bind_limit + 白名单 + 只读执行）
    """
    del task, context  # 未实现，仅占位
    return None


def solidify_sql(sql: str, task: str, hit_count: int = 1) -> Optional[SqlTemplate]:
    """回流固化（预留）：把 LLM 成功跑通的 SQL 去参数化后存入模板库。

    TODO(模板路由):
      1. 可固化判定：高频命中 / 参数可识别 / 无临时 hack
      2. 去参数化：字面量（日期/数字/引号字符串）→ 槽位，先规则后 LLM 辅助
      3. 入库前先过一次安全闸验证，人工审核或自动入库（带版本）
    """
    del sql, task, hit_count  # 未实现，仅占位
    return None
