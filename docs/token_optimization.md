# sweetAgent Token 优化方案

> 2026-10-03 · 基于项目现状分析（`app/agents/base.py`、`app/tools/sql/template_router.py`、`app/config/settings.py`）
> 关联日志：`docs/development_log.md` 考点七十二

---

## 一、现状：token 花在哪

一次典型问答（如"这周美国站销售和库存怎么样"）的调用链：

```
manager 规划 1 次
├─ operation ：plan 1 次 → 每个 req：SQL 生成 1 次 + analyze 1 次（+ repair 0~2 次）
├─ finance   ：同上
├─ logistics ：同上
└─ decision 汇总 1 次（带上各部门结果）
```

每个部门 2~4 个 req，**一次问答 ≈ 12~20 次 LLM 调用**。

### 消耗构成（估算）

| 消耗点 | 原因 | 占比（估算） |
|---|---|---|
| SQL 生成（`_build_schema_context`） | 每次注入最多 **6 张表完整 schema + 全部指标口径 + 业务字典** | ~35% |
| analyze（`_analyze`） | 每次把**全部查询结果 rows** 塞进 prompt | ~25% |
| repair 重试 | 失败时重新注入全量 schema 再生成 | ~15% |
| plan + decision | 规划 1 次 + 汇总 1 次 | ~20% |

> 比例是估算，未接计量。方向确定：**钱烧在"生成 SQL 喂太多 schema"和"分析塞太多数据"**。

### 关键认知

DeepSeek 单价低（百万 token 几块钱），但真实成本 = **调用次数 × prompt 大小** 的乘积：

- 砍**次数** → 模板路由 / skill 化（零 LLM 调用）
- 砍**大小** → schema / 结果裁剪

---

## 二、已经省到的点（不用动）

| 机制 | 状态 |
|---|---|
| chat_mode 聊天直答（非业务问题不空转部门） | ✅ 已实现 |
| invoke_structured 结构化输出（防格式幻觉与重试） | ✅ 已实现 |
| knowledge 数据域走 RAG（embedding 便宜） | ✅ 已实现 |
| tracking 数据域走 MCP（不烧 token） | ✅ 已实现 |
| SQL 结果缓存（表级失效索引） | ✅ 已实现（但只省 DB，不省 token） |

---

## 三、节省清单（按优先级）

### 1. 启用模板 SQL 路由（最大头，砍次数）

- **现状**：设计稿就绪（日志考点五十八/五十九），接口已预留（`_try_template` + `template_router.py` + `SQL_TEMPLATE_ROUTER_ENABLED` 开关默认 False），**只差实现**
- **原理**：高频固定口径（日销 / 退款率 / 库存风险 / 毛利率）命中模板 → 参数填充 → 直接执行，**零 LLM token**，SQL 生成 + repair 全部调用消失
- **附加收益**：成功 SQL 固化回流模板库（`solidify_sql` 预留点），正循环越用越省
- **工作量**：中（匹配器 + 参数填充 + 固化闸门，设计稿已有）
- **落点**：`app/tools/sql/template_router.py`、`BaseDepartmentAgent._try_template`

### 2. prompt 瘦身（第二大，砍大小）

| 子项 | 现状 | 优化 | 收益 |
|---|---|---|---|
| schema 按需裁剪 | 每次注入最多 6 张表完整 schema + 全量 metrics + 字典 | 只注入 req 相关 1~2 张表；schema 只留列名+类型（去注释/示例）；metrics 按关键词过滤 | SQL 生成省 50~70% |
| observations 裁剪 | 全量查询结果 rows 注入 | SQL 层先聚合（COUNT/SUM/GROUP BY）；注入前截断每表 ≤50~100 行；超大结果先统计再分析 | 大查询省 60~80% |
| repair 轻量化 | 重试时重复注入全量 schema | repair 用 small 模型 + 只带错误信息与字典 | 中 |

- **落点**：`BaseDepartmentAgent._build_schema_context`、`_analyze`、repair 分支

### 3. 分析结论缓存（砍全链路）

- **原理**：task + observations 指纹 → 命中直接复用上次分析结论，省 plan + SQL 生成 + analyze
- **关键**：复用现有表级失效索引（SQL 缓存同款）——数据更新自动失效，防过期结论
- **风险**：结论时效性。折中：短 TTL 或依赖表级失效
- **工作量**：中

### 4. 输出端限制（极小改动）

- `AnalysisOutput` 加条数上限：findings / anomalies ≤ 5，summary 限长（如 ≤500 字）
- 结构化 schema 已控字段，补条数即可
- **工作量**：极小

### 5. 模型分级（当前无收益，未来有用）

- **现状**：DeepSeek 三档配置全是 `deepseek-chat`，分级无差别
- **未来**：混合 provider（gpt-4o vs gpt-4o-mini、deepseek-chat vs deepseek-reasoner）后：plan / repair 用 small，复杂分析用 strong

### 6. 语义缓存（可选）

- 相同语义问题（embedding 相似）命中缓存，跳过整个 Agent 链路
- 收益大但复杂度中高，适合高频重复提问场景；优先级放最后

---

## 四、与"Skill 化"的关系（省 token 的终极形态）

**Skill = 把"每次让 LLM 现想"变成"直接调用现成能力"——确定性代码执行，零 LLM token。**

你的项目已经在走 skill 化路线（只是没叫这个名字）：

| 项目机制 | 本质 | 状态 |
|---|---|---|
| 模板 SQL 路由 | SQL 生成 skill | 设计就绪，待实现 |
| 非 LLM 意图/参数匹配（考点六十二/六十三） | 路由 skill（embedding + 正则 + 词典） | 已设计 |
| 能力域模板 NL2Cron（考点六十四补） | 任务创建 skill | 已设计/部分实现 |
| 模板 SQL 固化回流 | skill 的自我扩充（正循环） | 预留 |

**落地路线**：每新增一个"skill"，就把一次 LLM 调用换成确定性代码：

1. SQL 模板路由（第一个 skill）
2. 分析框架模板（高频分析类型固化 prompt + 确定性后处理）
3. 意图路由非 LLM 化（已设计）

---

## 五、落地路线图（建议顺序）

| 阶段 | 内容 | 改动范围 | 收益 |
|---|---|---|---|
| 阶段一 | prompt 裁剪（schema 按需 + observations 截断 + repair 轻量化） | 2~3 个函数 | 立即省 40~60% |
| 阶段二 | 模板 SQL 路由实现 + 固化回流 | template_router.py + _try_template | 高频问题零成本 |
| 阶段三 | 分析结论缓存（复用表级失效） | 新缓存模块 | 重复问题全链路免单 |
| 阶段四 | 输出条数限制 + 计量 | AnalysisOutput + 日志 | 小幅稳定 |

---

## 六、验证方式（先计量再优化）

1. **加 token 计量**：langchain 响应带 `usage_metadata`（prompt_tokens / completion_tokens），在 `invoke_structured` / `invoke_text` 统一记录，落 structlog
2. **指标**：每次问答的 LLM 调用次数、总 prompt tokens、总 completion tokens；按部门 / 按 req 类型拆分
3. **对比**：优化前后同问题集的 token 消耗（回归用例复用 `eval_regression` 的 load_cases）
4. **验收**：模板命中率（`hit_count`）、分析缓存命中率、单问答 token 降幅

---

## 七、结论

- token 大头在"SQL 生成的 schema 注入 + analyze 的结果注入"
- 最大省法：**启用已设计未实现的模板 SQL 路由**（零成本通道）
- 其次：**schema / 结果裁剪** + **分析缓存**
- 模型分级在纯 DeepSeek 配置下无收益，混合 provider 后再做
- skill 化（模板化/确定性化）就是省 token 的终极形态，项目已走在路上
