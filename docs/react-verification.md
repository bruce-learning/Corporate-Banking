# 有界 ReAct Agent 验证记录

验证日期：2026-09-20

版本：`1.0.0-bounded-react`

迁移：Alembic `0008 (head)`

## 实现范围

- Product、Process、Policy 三个领域 Agent 均可执行有界 ReAct。
- 控制器仅允许 `search_knowledge` 和 `finish`，检索始终绑定当前领域的只读 MCP 工具。
- 默认最多 3 个步骤、3 次工具调用、15 秒和 8 条去重证据。
- 重复检索、控制器无效输出、模型不可用、工具异常或超时都会停止并显式降级。
- 不保存或展示模型思维链，只保存动作、原因码、检索词、耗时和证据 ID。
- 外层 Fixed、Parallel 和 Plan-and-Execute 编排保持不变，ReAct 只在领域步骤内部运行。

## 接口与审计

- 聊天响应新增 `react_steps`。
- `chat_traces.react_steps` 使用 JSON 持久化，迁移文件为 `0008_react_traces.py`。
- 前端追踪面板显示领域、步骤、动作、原因码和检索词。
- Prometheus 文本指标新增 `bank_react_actions_total`、`bank_react_runs_total` 和 `bank_react_duration_seconds_*`。

## 验证结果

- 远程 Python：`3.10.8`。
- 完整自动化测试：`88 passed in 17.30s`。
- 覆盖多步补查、正常结束、重复检索停止、模型关闭回退、工具异常、全局超时、配置边界、API Trace 和迁移版本健康检查。

本次验证使用测试模型和合成知识，不代表真实银行资料上的回答质量；真实阿里云模型调用仍由 `ENABLE_PAID_MODEL_CALLS` 和服务端 API Key 控制。
