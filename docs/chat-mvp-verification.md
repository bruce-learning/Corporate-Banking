# 聊天编排 MVP 验收记录

验收日期：2026-09-20

版本：`0.6.0-chat-orchestration-mvp`

远程目录：`/root/autodl-tmp/project-python`

## 实现范围

`POST /api/v1/chat` 的实际调用链为：

```text
Bearer 鉴权 → 会话归属与 Redis 锁 → 凭据/交易风险硬规则
→ Pattern + 阿里云 Embedding + qwen-plus Few-shot 意图融合
→ 单领域或并行 Product / Process / Policy Agent
→ 官方 MCP SDK Streamable HTTP 客户端
→ product_search / process_lookup / policy_lookup
→ BM25 + ChromaDB 向量检索与有效版本过滤
→ 证据去重、引用检查、边界提示
→ PostgreSQL 消息、请求记录和 chat_traces 原子持久化
```

模型或向量服务不可用时，系统明确返回 `degraded=true`，并可退化到 BM25 与证据摘录；不会把退化答案伪装成正常模型答案。

## 自动验证结果

- 数据库迁移：Alembic `0004 (head)`；新增 `chat_traces`。
- 完整测试：`53 passed in 11.93s`。
- 合成意图集：98 条，7 类各 14 条；Accuracy 1.000，Macro-F1 1.000。
- 合成安全集：16 条；规则判定准确率 1.000；凭据脱敏失败 0。
- 既有检索基线沿用：360 条 Recall@3 98.06%、MRR@3 91.81%，60 条过滤负例零泄漏。
- 就绪检查：HTTP 200；PostgreSQL、Redis、ChromaDB、MCP 均为 true。

## 真实在线链路

使用服务器 `.env` 中的阿里云 Key（未读取、未输出）执行问题“不同产品是怎么收费的？”：

- 意图：`policy`；Agent：`policy`。
- 检索：`hybrid`；向量后端：`chromadb`；返回 4 条有效合成证据。
- 生成：`qwen-plus`，模式 `llm_grounded`，回答包含 `[资料:ID]` 引用。
- `degraded=false`，降级原因列表为空。
- 临时验收会话完成后删除；服务继续监听回环地址。

## 限制与边界

- 所有银行知识均为虚构合成演示数据，不代表任何真实银行产品、费率或审批政策。
- 1.000 指标来自模板化冻结回归集，不代表真实用户准确率；尚未完成真实风格脱敏集、人工盲评或大规模 LLM-as-Judge。
- 只读咨询，不连接核心银行系统，不代执行转账、付款、开户、销户、解锁，不收集验证码、密码和 U 盾口令。
- 人工转接目前只返回边界说明，没有真实工单或客服渠道集成。
- 当前无正式前端、OCR、生产级熔断器、Prometheus 看板、TLS 反向代理和专用非 root 运行账户。
- API Key、数据库密码、Redis 密码、访问令牌和 SSH 私钥不得写入代码、报告或聊天。

## 复现命令

```bash
cd /root/autodl-tmp/project-python
.venv/bin/python scripts/generate_chat_eval_dataset.py
.venv/bin/python scripts/evaluate_chat_mvp.py
.venv/bin/python -m pytest -q
.venv/bin/python scripts/chat_smoke.py
curl http://127.0.0.1:8000/ready
```
