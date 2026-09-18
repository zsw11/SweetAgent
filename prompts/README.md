# Prompt 版本管理（设计文档 46 节）

每个 Agent 一个目录，按版本存放 `v1.md`、`v2.md`：

```text
prompts/
├── manager/
├── operation/
├── logistics/
├── finance/
├── product/
└── decision/
```

配套数据库表 `prompt_versions`（id / agent_name / version / content_hash / created_at / is_active），
用于回答「为什么昨天 Agent 结果和今天不一样」。
