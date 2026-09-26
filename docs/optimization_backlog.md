# SweetAgent 优化清单

> 来源：2026-09-23「AI Agent 岗位能力培养与竞争力构成」架构图对照自评（能力覆盖矩阵 8/15 ✅）。
> 优先级：**P0** 招聘核心技能缺口 / 最大价值；**P1** 补强已有能力；**P2** 底座补齐（可后续做）。
> 状态图例：⬜ 待办 · 🚧 进行中 · ✅ 完成

---

## P0 —— 招聘核心技能缺口（最大价值）

### OPT-01 MCP 接入（**✅ 已完成 2026-09-24**，Client 方向）
- **落地**：tracking 数据域走 MCP streamable 接快递100（`app/tools/logistics_tracking.py` + base.py `_query_tracking`）；动态发现 5 工具，auto_number 识别承运商 + query_trace 查轨迹，verify 三场景通过；详见 development_log.md 考点三十一。
- **两种角色**：① **Client（调用外部 MCP）——本项目主方向，已落地**；② Server（把内部工具开放给别人）——当前无多客户端需求，非本项目方向。
- **触发条件**（主要）：业务要接**外部数据源**（物流跟踪 / 汇率 / 电商平台 API / 行业基准等）——对跨境电商分析是自然扩展；次要：内部工具需被第二个客户端复用。
- **前置**：OPT-05 Function Calling 原生化（MCP 本质是 function calling 的标准化协议，先标准化内部工具，接外部工具时链路更顺）。
- **最小实验方案（若仅为求职证据）**：用 `MultiServerMCPClient` 接入 1 个官方 reference server（如 fetch/time），在独立脚本里验证工具可被调用（约半天，不接主图）。
- **预估工作量**：M（1~2 天，条件触发后）
- **验收标准**：Agent 通过 MCP Client 完成一次外部工具调用（如物流查询/汇率换算），结果进入 observations 供决策消费。

### OPT-02 LangSmith 可观测性（待办主线）
- **现状**：自建 logging/metrics 已有；LangSmith 接入是当前待办主线，需先确认三问：① API Key；② langsmith.com 网络可达性；③ 业务数据是否允许上云（敏感则降级为仅本地 prompt 版本化）。
- **目标**：Agent 全链路 trace（manager→部门→decision→quality_gate）+ 评测数据回流。
- **做法**：接入 `langsmith` SDK / LangGraph callback；`project_name=sweetagent`；评估结果上报为 dataset。
- **涉及范围**：`app/graph/main_graph.py`（编译时配置）、`app/observability/`。
- **预估工作量**：S（半天~1 天，取决于三问确认）
- **验收标准**：一次真实提问在 LangSmith 面板可见完整 trace 树（含 LLM 调用、工具调用、quality_gate 判定）。

### OPT-03 评测报告正式化（**✅ 已完成 2026-09-24**）
- **落地**：新增 `scripts/eval_report.py`——读 evaluation_runs/scores/cases，汇总批次对比 + 最新批次明细 + bad case 归因 + 费用耗时，输出 Markdown 到 `docs/eval/`；渲染/bad case 提取已 mock 验证，DB 不可用优雅降级提示。用法：`run_evaluation.py` 跑完 → `eval_report.py` 一键出报告。
- **待办（可选）**：MCP tracking 用例加入用例库（`expected_sql_pattern` 加 `mcp:` 标注）；质量门触发统计需 run_evaluation 先落库质量门字段。

---

## P1 —— 补强已有能力

### OPT-04 结构化输出强化（**✅ 已完成 2026-09-24，全量接入 2026-09-25**）
- **落地**：统一封装层 `app/llm/structured.py`（invoke_structured / invoke_tool / invoke_text / extract_json），8 处 LLM 调用点全部接入——base._plan/_analyze、decision._synthesize、manager._plan、memory extractor/judge、graph.quality、sql generator（工具通道）。manager 抽公共 `_validate_plan`、judge 抽 `_normalize_judge`。详见 development_log.md 考点三十四。
- **关键坑**：langchain-openai 1.6+ with_structured_output 默认 json_schema，DeepSeek 不支持（400）→ 显式 method="function_calling" 并真实冒烟通过。
- **待办（可选）**：上真实业务回归。

### OPT-05 Function Calling 原生化（**✅ 已完成 2026-09-24**）
- **落地**：SQL 生成/修复走原生 tool_calls——`generator.py` 新增 `_SQL_TOOLS`（generate_sql / repair_sql 两个 function schema）与 `_invoke_sql()`：`bind_tools` 强制单工具调用，SQL 从 `tool_calls[0].args` 结构化提取（不再依赖文本 + markdown 剥离），模型不支持/API 异常自动降级文本回复。verify_opt04_05.py 场景 A/B/G 通过（G 为 DeepSeek 真实 bind_tools 冒烟）。
- **扩展点（✅ 2026-09-24 已落地并行执行）**：logistics/graph.py `_query` 已改 `ThreadPoolExecutor` 并行（max_workers=min(未查数,4)，observations 按 plan 顺序收集保序，单域异常隔离 + queried 防重试），verify_logistics_parallel.py 验证：4 域串行 1.6s → 并行 0.41s。knowledge RAG 检索同样可声明为 tool 走同一通道（未做）。
- **涉及范围**：`app/tools/sql/generator.py`、`app/agents/logistics/graph.py`。
- **验收标准**：工具调用不再依赖手写 JSON 格式；bad case 中"工具格式错误"归零 —— 通道层已就绪，bad case 验证待真实业务回归。

### OPT-06 提示注入 / 输出安全
- **现状**：SQL 侧安全已有（ReadOnlyExecutor 只读 + validator 校验 + 参数化），LLM 输入/输出侧未系统化防护。
- **目标**：识别并防御提示注入（用户问题夹带指令）与敏感数据外泄。
- **做法**：① 注入检测规则（system 边界声明 + 高危指令词命中标记）；② 输出脱敏（手机号/邮箱/金额掩码）策略位；③ 记录为可观测事件。
- **涉及范围**：`app/observability/`、`app/graph/main_graph.py` 入口。
- **预估工作量**：M
- **验收标准**：注入用例被标记且不污染决策；脱敏规则覆盖 3 类敏感字段。

### OPT-07 查询 / RAG 缓存
- **现状**：同问题重复查询重复走 SQL / 向量检索，无缓存。
- **目标**：降低延迟与 token/DB 成本。
- **做法**：SQL 结果缓存（task hash + 表级失效）与 RAG 检索缓存（query embedding hash + top-k 缓存），TTL 可配；监控命中率。
- **涉及范围**：`app/tools/sql/executor.py`、`app/knowledge/retriever.py`。
- **预估工作量**：S~M
- **验收标准**：同问重复调用命中缓存（日志可证）；命中率指标可观测。

---

## P2 —— 底座补齐（可后续做）

### OPT-08 容器化
- **现状**：本地开发为主，未见 Dockerfile / compose。
- **目标**：一键起环境（应用 + Postgres/pgvector）。
- **做法**：`Dockerfile`（多阶段）+ `docker-compose.yml`（app / postgres-pgvector），`.env` 模板化。
- **预估工作量**：M
- **验收标准**：`docker compose up` 后 health 通过，可完成一次提问。

### OPT-09 异步任务化
- **现状**：`POST /chat` 同步等待完整链路（10~60s），Web 请求长阻塞。
- **目标**：提交即返回 task_id，轮询/SSE 获取进度。
- **做法**：任务队列（Celery/RQ 或 asyncio 后台任务）+ 状态表；SSE 推送阶段进度（Phase 9 已有铺垫）。
- **预估工作量**：L
- **验收标准**：请求 <1s 返回；前端可看到 planning→running→done 阶段流。

### OPT-10 Skills / A2A
- **现状**：无技能编排（Skills）与跨 Agent 通信协议（A2A）。
- **目标**：MCP 落地后的自然延伸——Agent 可声明/加载 Skills；多 Agent 间按协议协作。
- **做法**：先做 Skills（把 SQL/知识/记忆封装为可加载技能清单），A2A 视需求再评估。
- **预估工作量**：L
- **验收标准**：Agent 可按任务动态加载不同 Skills 组合；A2A 至少一个跨 Agent 调用场景跑通。

---

## 建议执行顺序

| 批次 | 项目 | 理由 |
|---|---|---|
| 第一批 | OPT-03 评测报告 → OPT-02 LangSmith | 成本最低、双收（质量+证据），且 LangSmith 依赖三问确认可并行 |
| 第二批 | OPT-01 MCP | **条件触发**：Client 方向优先——接外部数据源（物流/汇率/平台 API）时做；仅求职证据先做最小实验（OPT-05 前置） |
| 第三批 | OPT-04/05/07 | 结构化输出、工具原生、缓存——纯内部质量提升 |
| 第四批 | OPT-06 安全 → OPT-08 容器化 | 安全与交付形态 |
| 第五批 | OPT-09/10 | 架构级，建议评估后再排期 |

## 状态跟踪

| 编号 | 项目 | 优先级 | 工作量 | 状态 |
|---|---|---|---|---|
| OPT-01 | MCP 接入 | P0 | M | ✅（2026-09-24） |
| OPT-02 | LangSmith 可观测性 | P0 | S | ⬜（待三问确认） |
| OPT-03 | 评测报告正式化 | P0 | S | ✅（2026-09-24） |
| OPT-04 | 结构化输出强化 | P1 | S~M | ✅（2026-09-24） |
| OPT-05 | Function Calling 原生化 | P1 | M | ✅（2026-09-24） |
| OPT-06 | 提示注入/输出安全 | P1 | M | ⬜ |
| OPT-07 | 查询/RAG 缓存 | P1 | S~M | ⬜ |
| OPT-08 | 容器化 | P2 | M | ⬜ |
| OPT-09 | 异步任务化 | P2 | L | ⬜ |
| OPT-10 | Skills / A2A | P2 | L | ⬜ |
