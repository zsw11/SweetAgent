# sweetAgent 工具安全与误调用防线

> 2026-10-03 · 基于项目现状分析（`app/agents/base.py`、`app/tools/`、`app/scheduler/`）
> 关联日志：`docs/development_log.md` 考点七十三
> 核心结论：**工具要路由归档，LLM 按路由方向调用；参数要结构化加校验**

---

## 一、问题定义：工具误调用长什么样

| 误调用类型 | 结合项目实例 | 后果 |
|---|---|---|
| 误选数据域/工具 | 该查 sales_sku 却查 review/ad；该走 SQL 却调 tracking MCP | 结果错 + 烧 token/钱 |
| 参数错误 | 品牌 US↔UK、日期窗口倒置、SKU 编造 | 结果错 / 查询失败 |
| 不该调用时调用 | 闲聊却查库；没提物流却触发外部 MCP | 浪费 + 烧外部额度 |
| 重复调用 | 同一轮内同数据域查两次 | 浪费 token |
| 危险调用 | SQL 写操作、误创建定时任务 | 数据/副作用风险 |

### **核心原则**： **_首先不能再LLM里面执行工具调用和业务，工具调用和业务必须在代码里_**
                  不能靠"提示 LLM 别调错"——LLM 输出不可信，要用**确定性代码定边界**（白名单哲学，与 SQL 安全同一套）。

## 二、设计一：工具路由归档（LLM 按路由方向调用）

### 2.1 工具注册表（ToolSpec）

每个工具在注册表中声明五件事，**Agent 只能调注册表内的工具**：

```python
@dataclass
class ToolSpec:
    tool_id: str                      # 工具标识（execute_sql / rag_search / tracking_mcp ...）
    department: str                   # 归属部门白名单（防跨部门调用）
    data_domains: list[str]           # 可服务的数据域（对接 KNOWN_REQS）
    trigger: str                      # 场景触发条件（何时才允许调）
    params_schema: type[BaseModel]    # 参数 Pydantic Schema（结构化）
    dictionary_keys: list[str]        # 需过词典的字段（品牌/市场/SKU）
    cost_level: str                   # low / mid / high（tracking MCP = high）
    require_explicit: bool            # 高成本工具：需用户显式意图才放行
    handler: callable                 # 实际执行函数
```

示例（跟踪 MCP——最典型的"高成本外部调用"）：

```python
ToolSpec(
    tool_id="tracking_mcp",
    department="logistics",
    data_domains=["tracking"],
    trigger="用户显式提到：单号 / 物流 / 快递 / 轨迹",
    params_schema=TrackingParams,      # tracking_no: 必填, 长度/格式校验
    dictionary_keys=[],
    cost_level="high",                 # 外部 API，花钱
    require_explicit=True,             # 必须显式意图才放行
    handler=LogisticsTrackingClient().track,
)
```

### 2.2 路由判定流程（非 LLM 优先，LLM 只做受限补充）

```
用户问题
  ↓
① 确定性路由（非 LLM）：embedding + 词典 + 规则 → 候选工具列表
   （复用考点六十二/六十三：向量余弦 + 枚举词典召回）
  ↓
② 门控过滤：候选工具过 trigger 条件
   （tracking 无"单号/物流"词 → 直接剔除，连候选都不进）
  ↓
③ LLM 受限选择：只能在候选内选工具 + 抽取参数（不是自由选择）
   ↓
④ 参数闸门：Pydantic Schema + 词典校验 + 日期边界
   ↓
⑤ 执行：白名单 + 只读角色 + sqlglot（统一安全闸）
   ↓
⑥ 结果校验 + 审计日志（agent/tool/args/结果摘要）
```

**关键转变**：工具选择从"LLM 自由选"变成"确定性路由召回 + LLM 受限选择"——LLM 选错的空间被路由层大幅压缩。

### 2.3 与现有白名单的关系

| 现有 | 升级为 |
|---|---|
| KNOWN_REQS 数据域白名单（只过滤） | ToolSpec 注册表（声明域 + 门控 + schema，可纠正） |
| `_extract_plan` 丢弃白名单外域 | 路由层直接召回正确域（不再等 LLM 先错再丢） |
| FALLBACK_REQ 兜底 | 路由兜底（无候选 → 走主图 chat 直答 / 拒绝） |

---

## 三、设计二：参数结构化 + 校验

### 3.1 三层参数校验（NL2Cron 已验证的模式，推广到所有工具）

1. **Pydantic Schema**：类型 / 枚举 / 必填——`invoke_structured` 或函数调用参数直接约束
2. **词典校验**：品牌 / 市场 / SKU 过 `_load_dictionary()` 预置词典（如 "US" 合法、"UK" 不合法 → 拒绝或纠正重试）
3. **边界校验**：日期窗口（结束 > 开始、窗口 ≤ 90 天）、数值范围（limit 1~1000）

### 3.2 失败策略

- 拒绝（不执行）或纠正重试（给 LLM 反馈"品牌不在词典，候选：US/MY/PH"）
- 关键：**校验失败不放行**——宁可拒绝，不可错跑（与 NL2Cron"宁可拒绝不可静默错跑"同一哲学）

---

## 四、五层纵深全景

```
用户问题
   ↓
① 路由层   确定性路由召回（embedding+词典+规则）→ 候选工具      【防误选】
   ↓
② 参数层   Pydantic Schema + 词典 + 日期边界                     【防参数错】
   ↓
③ 门控层   高成本工具显式意图；写操作/外部调用人工确认            【防烧钱/副作用】
   ↓
④ 执行层   白名单 + 只读角色 + sqlglot + statement_timeout       【防危险调用】
   ↓
⑤ 结果层   空/异常结果 repair；decision 跨部门交叉验证           【兜底纠错】
   ↓
⑥ 反馈层   误调用日志 + 点踩 → 半自动转正 → 回归用例             【正循环】
```

**项目现状对照**：

| 层 | 状态 |
|---|---|
| ④ 执行层 | ✅ 已就位（sqlglot + 只读角色 + 白名单 + 超时） |
| ⑤ 结果层 | ✅ 部分（row_count==0 触发 repair）；补：异常大结果、跨部门口径一致性 |
| ⑥ 反馈层 | ✅ 骨架已就位（点踩回流）；补：工具调用审计日志 + 误调用信号类别 |
| ① 路由层 | ⏳ 设计稿就绪（考点六十二/六十三），未实现 |
| ② 参数层 | ⏳ NL2Cron 有设计，工具层未推广 |
| ③ 门控层 | ⏳ 未实现（tracking MCP 无显式门控） |

---

## 五、落地路线图（优先级）

| 优先级 | 内容 | 落点 | 收益 |
|---|---|---|---|
| 1 | 非 LLM 路由（embedding+词典定数据域/工具） | 复用考点六十二/六十三设计 | 从根减少误选 |
| 2 | 参数 Schema + 词典闸门推广到所有工具 | tools/ 各工具加 params_schema | 拦参数错误 |
| 3 | 高成本工具显式门控（tracking MCP） | ToolSpec.require_explicit | 防烧外部额度 |
| 4 | 误调用案例回流到回归 | evaluation_harvest 扩展信号类别 | 持续改进 |
| 5 | 工具调用审计日志 | observability + 每工具调用记录 | 可观测、可追查 |

---

## 六、验证方式

1. **误调用率指标**：每百次工具调用的拒绝/纠正/丢弃次数（审计日志统计）
2. **回归用例**：往 `evaluation_cases` 加"误调用陷阱"用例（含闲聊问物流、编造品牌、倒置日期等），`eval_regression` 必须拦住
3. **成本对比**：tracking MCP 调用次数（门控前后）、外部 API 额度消耗
4. **端到端**：真实 LLM 跑"该查库存却问物流轨迹"类问题，验证路由层正确召回、门控正确拦截

---

## 七、结论

- **工具路由归档**：Agent 只能调注册表内的工具，路由层（非 LLM）召回候选 + 门控过滤，LLM 只做受限选择——误选空间被确定性代码压缩
- **参数结构化 + 校验**：Pydantic Schema + 词典 + 边界三层闸门，失败拒绝或纠正，不放行
- 五层纵深里 ④ 执行层、⑥ 反馈骨架已就位，主要补 ①②③；设计稿（考点六十二/六十三）已在日志，落地路径清晰
