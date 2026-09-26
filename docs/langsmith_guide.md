# LangSmith 使用指南（sweetAgent · OPT-02）

> 版本：2026-09-26 · 对应代码：`app/observability/tracing.py` · 验证脚本：`scripts/verify_langsmith.py` · 上报脚本：`scripts/upload_eval_dataset.py`

## 1. LangSmith 是什么

LangSmith 是 LangChain 官方的 **LLM 应用可观测性平台**，对用了 LangChain / LangGraph 的项目，它解决三件事：

| 能力 | 解决什么 | 对应面板 |
|---|---|---|
| **Tracing（链路追踪）** | 一次提问从进来到出去，每一步 LLM 调用、工具调用、图节点、判定的输入/输出/耗时/token 全记录 | Projects → Traces |
| **Datasets & Testing（评测回流）** | 把评估结果、bad case 组织成数据集，多批次跑回归对比 | Datasets / Testing |
| **Monitoring（监控）** | 生产环境按项目聚合：错误率、耗时 P50/P99、token 消耗趋势 | Monitoring |

**为什么用它的核心原因**：LLM 应用是"管道里有随机性"的系统——同样的问题可能这次回答好、下次回答差。没有 tracing，你只能靠日志猜；有了 tracing，每次回答都能还原成一条完整的因果树（哪个节点用了什么输入、模型怎么想的、工具返回了什么、质量门怎么判的）。

对本项目，验收标准是：**一次真实提问，面板上能看到完整 trace 树——manager → router → 部门（operation/finance/... 并行）→ decision → quality_gate 判定**。

---

## 2. 本项目接入了什么（30 秒上手）

### 2.1 配置（.env 三行）

```ini
LANGSMITH_TRACING=true            # 总开关：true=自动上报 trace
LANGSMITH_API_KEY=lsv2_pt_...     # 你的 API Key
LANGSMITH_PROJECT=sweetagent      # trace 归属的项目名（面板左侧 Projects 列表）
```

### 2.2 代码只有两个入口挂了一次初始化（幂等，重复调用零副作用）

- `app/observability/tracing.py` → `init_langsmith()`：把 .env 配置**同步回环境变量**（LangChain tracer 和 langsmith SDK 只认环境变量，而 pydantic-settings 读 .env 不会写回），然后初始化并验证 Client。**任何 LLM/图调用前必须执行过它**。
- `app/main.py`（FastAPI 启动）+ `app/graph/main_graph.py` 的 `run_question()` 开头都调了它——Web 服务和脚本/测试路径都能上报。
- 关键点：**业务代码零侵入**。tracing 由 LangChain/LangGraph 的自动 tracer 完成，不需要给每个节点加埋点。

### 2.3 两个脚本

```bash
# 验证：①key/网络/项目可见 ②真实提问全链路 trace 上云并打印 URL
.venv\Scripts\python scripts\verify_langsmith.py --run 1

# 上报：把最近一次完成的评估批次回流成 dataset（幂等，可反复跑）
.venv\Scripts\python scripts\upload_eval_dataset.py
```

---

## 3. 面板怎么用（从打开到读懂）

### 3.1 进入

浏览器打开 `https://smith.langchain.com`，用 LangChain 账号登录（API Key 在 **Settings → API Keys** 里创建/轮换）。

### 3.2 Projects 列表

左侧 **Projects** 下会有 `sweetagent`（首次上报 trace 时自动创建）。每个 project 是一块独立领地：项目名不同，trace 互不混。

### 3.3 读一条 trace 树（核心技能）

点进 project → **Traces** 标签，列出一批提问，每个是一条 run（树根）。点开任意一条，你会看到一棵树，对应本项目实际链路：

```
run_question                        ← 顶层：这次提问
├── manager                         ← 规划：理解问题→任务拆解→DAG
│   └── ChatOpenAI (deepseek-chat)  ← LLM 调用（结构化输出通道）
├── router                          ← 路由决策
├── operation                       ← 部门节点（并行分支之一）
│   ├── ChatOpenAI                  ← SQL 生成（function calling）
│   ├── execute_readonly_sql        ← 工具调用（SQL 执行）
│   └── ChatOpenAI                  ← 分析结构化输出
├── finance                         ← 部门节点（并行分支之二，和 operation 同层并行）
│   └── ...（同样结构）
├── decision                        ← 汇总各部门 → 最终报告
│   └── ChatOpenAI
└── quality_gate                    ← 质量门判定（pass/回炉/放行）
    └── _quality_route
```

每个节点点开能看到：
- **Input / Output**：发给 LLM 的完整 prompt、返回的原始输出（含 tool_calls 的 JSON）
- **耗时与 token**：该步耗时、输入/输出 token 数
- **错误**：节点报错会标红，错误信息直接可见（排查 bad case 的第一站）

**读树的三个抓手**：
1. **找红色节点** = 报错/异常，先看它；
2. **找高耗时节点** = 性能瓶颈（比如某次 SQL 修复重试了 2 次）；
3. **对比 Input 看"模型想岔在哪"** = 定位回答质量问题的根因（比如 router 规划漏了部门）。

### 3.4 用 Filter 筛 bad case

Traces 页有过滤条，常用筛选：
- `error`：只看报错的 run
- `latency >= 20`：只看慢请求
- `has feedback`：只看有人工反馈的（如接入了 Feedback API）
- 按节点名 / token 数 / 日期范围都可以筛

### 3.5 Datasets 与 Testing（回归评测）

- **Datasets**：`sweetagent-eval` 数据集已存在，里面每条 example = 一条评估用例（question + 三维得分 + case 元信息）。`upload_eval_dataset.py` 每次跑会按 `case_id` 幂等更新，所以**多批次评估后，同一用例在 dataset 里能看到各版本分数变化**（坏例是好是坏、是否反复）。
- **Testing**：如果你想在面板上"点一下跑回归"，可以把数据集和一个模型/agent 关联，面板会批量执行并出对比报告。本项目离线评估走本地 `run_evaluation.py`（省钱、可重放），面板 Testing 是可选的云端执行方式。

### 3.6 Monitoring（可选）

把 `LANGSMITH_TRACING=true` 常驻开启后，Monitoring 会自动聚合：每日请求量、错误率、平均/最大耗时、token 消耗曲线。适合上线后看趋势。

### 3.7 Feedback（可选）

生产系统可以在用户"点赞/点踩"时调用 `client.create_feedback(run_id, key="user_score", score=...)` 把人工反馈挂到对应 trace 上，之后面板可按反馈筛选。本项目尚未接入，属后续可选项。

---

## 4. 日常工作流（怎么把 LangSmith 用起来）

```
改代码 / 加功能
   ↓
跑一次真实提问（webui 或 verify_langsmith.py --run 1）
   ↓
面板 Projects → sweetagent → 看这条 trace：
   链路是否按预期走？每个 LLM 调用输入输出对不对？耗时 token 是否合理？
   ↓
跑评估批次：run_evaluation.py --case 6,10（或 --all）
   ↓
上报：upload_eval_dataset.py
   ↓
面板 Datasets → sweetagent-eval：对比历史批次分数，看 bad case 修没修好
```

**要点**：
- 每次功能改动，先看 trace 再下结论，别靠猜；
- bad case 排查路径：trace 树里找红节点/可疑节点 → 看该步 Input/Output → 定位是规划错、SQL 错还是判定错 → 改完重跑同一用例 → dataset 里对比分数。

---

## 5. 开关与配置细节

| 场景 | 做法 |
|---|---|
| 临时关掉 tracing | `.env` 里 `LANGSMITH_TRACING=false`（日志/metrics 不受影响） |
| 多个环境隔离 | 每个环境独立 `.env`，PROJECT 名区分（如 `sweetagent-dev` / `sweetagent-prod`） |
| 数据敏感 | 开启后 LLM 输入输出原文会上云；涉敏场景可关 tracing，或只对非敏感项目开 |
| 换 Key | LangSmith 面板 Settings → API Keys 生成新 key，改 `.env` 即可（旧 key 可作废） |

---

## 6. 安全与成本提醒

- **Key 是敏感凭据**：`.env` 已在 `.gitignore` 中（`git status` 确认不会被提交）；不要把 key 发到聊天/文档/截图里。
- **数据上云**：tracing 会把提问和回答原文传到 `api.smith.langchain.com`。本项目种子数据为模拟数据，风险可控；若接真实业务数据，先评估合规再开。
- **免费额度**：LangSmith 个人免费版有每月 trace 数/token 上限，超限会影响上报（一般不影响主链路，日志有 `langsmith.*` 降级记录）。

---

## 7. 常见问题（FAQ）

**Q1：面板看不到 trace？**
依次查：① `.env` 是否 `LANGSMITH_TRACING=true` 且 Key 正确；② 是否在第一次 LLM 调用前执行了 `init_langsmith()`（`run_question` 已内置）；③ 网络能否访问 `api.smith.langchain.com`；④ 日志里 `langsmith.ready` / `langsmith.init_fail` 显示什么。

**Q2：trace 上传失败会影响主链路吗？**
不会。`init_langsmith()` 全程 try/except，失败只打 `langsmith.init_fail` 警告降级，业务照常。**可观测性永远不能拖垮业务**。

**Q3：dataset 重复上报会建重复 example 吗？**
不会。每个用例的 example_id 由 `case_id` 稳定生成（uuid5），已存在则走更新分支，幂等。

**Q4：为什么 trace 里没有 LLM 调用？**
该调用没走 langchain 通道（如 `httpx` 直连、原生 SDK 直调）就不会被 tracer 捕获。本项目 8 处 LLM 调用全走 `app/llm/structured.py` 封装，所以都能看到。

**Q5：验证脚本里的 URL 打不开？**
URL 是 `smith.langchain.com/o/<org>/projects/p/<project>/r/<run_id>`，需要登录同一账号；个别 run 数据未就绪时加 `?poll=true` 会自动等待。

---

## 8. 本项目接入点速查（代码地图）

| 文件 | 职责 |
|---|---|
| `app/observability/tracing.py` | 配置同步 + Client 初始化 + run 查询辅助（唯一入口） |
| `app/main.py` | FastAPI 启动时初始化 |
| `app/graph/main_graph.py` | `run_question()` 开头初始化（脚本/测试路径兜底） |
| `scripts/verify_langsmith.py` | 验证 key/网络/项目 + 真实提问 trace 上云 |
| `scripts/upload_eval_dataset.py` | 评估结果回流 dataset（幂等） |
| `.env` | `LANGSMITH_TRACING / API_KEY / PROJECT` 三配置 |
