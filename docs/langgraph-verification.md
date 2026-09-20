# LangGraph 多 Agent 编排验证记录

验证日期：2026-09-20

版本：`1.1.0-langgraph`

迁移：Alembic `0009 (head)`

## 实现范围

- LangGraph `StateGraph` 作为默认编排后端，旧版 `BankOrchestrator` 通过配置保留回滚能力。
- 主图包含风险检查、查询改写、意图识别、固定回答、任务规划、领域执行和证据聚合节点。
- Product、Process、Policy 使用同构领域子图；复合问题通过动态 `Send` 并行分发。
- `plan_execute` 按既有 `depends_on` 顺序执行，并把上一步领域答案作为下一步上下文。
- 领域子图继续使用现有有界 ReAct、MCP 只读工具、动态 Skills 和执行预算。
- Reducer 聚合领域结果和节点 Trace；聚合节点完成证据去重、冲突检查、引用检查和降级归并。
- `ORCHESTRATION_BACKEND=legacy` 可切回旧编排器；LangGraph 运行时异常会显式进入只读旧链路回退。

## 接口与审计

- 聊天响应新增 `orchestration_backend` 和 `graph_trace`。
- `chat_traces` 通过 Alembic 0009 持久化编排后端及节点 Trace。
- 前端追踪面板显示 LangGraph 节点、领域、状态与原因码。
- 就绪与管理员状态接口公开当前编排后端。
- Prometheus 文本指标新增 `bank_langgraph_runs_total`。

## 验证结果

- 远程 Python：`3.10.8`。
- LangGraph：`1.2.11`。
- LangGraph 与聊天核心测试：`10 passed in 2.82s`。
- 完整自动化测试：`92 passed in 19.21s`。
- 并行测试观测到 Product、Policy、Process 三个领域同时执行；顺序测试保持 Product → Policy → Process。
- 安全阻断在意图、模型和工具调用之前完成。

这些结果证明当前合成演示环境的编排和回归状态，不代表真实银行生产稳定性或业务正确率。
