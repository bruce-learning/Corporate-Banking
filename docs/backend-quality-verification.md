# 后端质量门禁验收记录

验收日期：2026-09-20

版本：`0.7.0-backend-quality-gate`

远程目录：`/root/autodl-tmp/project-python`

## 新增后端能力

- 多轮代词补全和查询改写，原问题与改写问题分别审计。
- `fixed`、`parallel`、`plan_execute` 三种执行模式。
- 复杂任务仅按 Product → Policy → Process 白名单顺序执行，不允许任意工具或交易操作。
- MCP 有限重试、Redis TTL 缓存、熔断/半开、超时分类和本地 HybridSearch 回退。
- 引用 ID 白名单校验、无效引用移除、引用合法性和领域覆盖率记录。
- 模型操作、模型名和 Token 数持久化；不记录 API Key、密码、令牌或完整敏感正文。
- 数据库后台导入任务、失败重试、服务重启中断恢复、PostgreSQL/ChromaDB 一致性检查。
- EvaluationRun、BadCase、ModelUsageRecord 和扩展 ChatTrace 数据表。
- `/metrics` Prometheus 文本指标和管理员观测汇总接口。

## 数据库与自动测试

- Alembic：`0005 (head)`。
- 全量测试：`63 passed in 15.43s`。
- 代码语法检查：通过。
- 业务就绪：HTTP 200；PostgreSQL、Redis、ChromaDB、MCP 全部为 true。

## 冻结离线质量门禁

数据版本：`backend-quality-v1`，全部为组合式合成数据。

| 数据集 | 数量 | 第二轮结果 |
|---|---:|---:|
| 意图分类 | 525 | Accuracy 1.000，Macro-F1 1.000 |
| 安全规则 | 120 | 1.000 |
| 多轮改写 | 150 | 1.000 |
| 复杂计划 | 90 | 1.000 |
| 降级契约 | 100 | 已冻结并由故障测试覆盖 |
| Prompt Injection | 80 | 拦截率 1.000 |

首轮意图评测 Accuracy 0.9714、Macro-F1 0.9711，发现15条同源 Bad Case：
“审批有效期多久”中的“多久”被误当作办理流程信号。移除该弱强制路由信号后，第二轮525条全部通过，
首轮15条 Bad Case 在 PostgreSQL 中自动从 `open` 更新为 `fixed`。这保留了发现、修复和回归证据。

以上1.000仅适用于冻结合成组合集，不代表真实客户分布。

## 真实在线验收

### 复杂任务

问题：“请先说明企业网银的功能，再说明收费限制，之后给出办理步骤”。

- 意图：composite；执行：plan_execute。
- 顺序：Product → Policy → Process，三步均 success。
- 三个工具均为 hybrid/ChromaDB，共12条证据。
- 首次来自 MCP，第二次相同查询来自 Redis cache。
- qwen-plus grounded 生成；引用合法率1.0，引用领域覆盖率1.0。
- 最终 `degraded=false`。

首次在线运行暴露了模型生成未知引用的问题，当时系统正确标记 `citation_invalid` 并降级。修复为删除未知引用并补充有效来源后，复测通过。

### MCP 故障回退

短暂停止项目专属 MCP 后执行收费问题：

- MCP 共尝试2次。
- 自动转入 `local_fallback`。
- 本地仍通过 ChromaDB hybrid 返回4条有效证据。
- qwen-plus 生成带引用回答。
- 明确返回 `degraded=true` 和 `mcp_unavailable`，没有伪装成正常 MCP 调用。
- 验收后 MCP 已恢复，全部服务运行正常。

### 有限并发

对安全硬拦截链路执行20并发请求：

- 成功率：1.0。
- P50：482.84ms。
- P95：496.98ms。
- 模型调用：0。
- 临时会话全部删除。

该结果只覆盖数据库、Redis、鉴权、会话锁和安全规则，不代表付费模型问答的并发容量。

## LLM-as-Judge 与人工复核边界

4条真实在线合成问题的小样本 Judge：

| 维度 | 均值 |
|---|---:|
| 相关性 | 1.000 |
| 完整性 | 0.9875 |
| 忠实度 | 0.975 |
| 安全性 | 1.000 |
| 引用合法率 | 1.000 |
| 引用覆盖率 | 1.000 |
| 降级率 | 0.000 |

生成模型和 Judge 都是 qwen-plus，只有4条问题，存在明显同模型偏差。已生成
`evaluation/human_review/answer-judge-v1-blind.jsonl`，其中不包含 Judge 分数，但尚未由独立人员完成评分。
因此这些分数只能作为链路冒烟，不能宣传为生产回答质量。

## 后端边界

- 所有知识和评测问题均为合成演示数据。
- 不连接真实银行核心系统，不执行转账、付款、开户、销户、解锁或审批。
- 后台导入使用当前 FastAPI 进程执行；任务状态持久化，进程中断后标记为可重试失败，但不是外部任务队列。
- `/metrics` 为进程内指标，服务重启后计数重置；模型用量与评测结果另行持久化到 PostgreSQL。
- 扫描 PDF OCR、真实人工转接、真实标注数据、独立人工盲评、TLS、专用非 root 账户和外部告警不在本阶段完成范围。
- 前端按用户要求暂缓。
