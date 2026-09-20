# RAG 与标准 MCP 基础阶段验证报告

验证日期：2026-09-19

验证位置：远程持久化目录 `/root/autodl-tmp/project-python`

## 已验证结果

- 远程完整测试：`45 passed in 9.01s`。
- 数据库迁移版本：`0003_knowledge`；文档、知识分块和索引版本均由 PostgreSQL 持久化。
- 知识建库：180 条有效合成知识、60 条过滤控制记录，18 次阿里云 Embedding 请求，向量维度 1024。
- 免费 BM25 基线：360 题 Recall@3 `97.50%`、MRR@3 `86.06%`、控制记录零泄漏。
- 正式混合检索：BM25 + ChromaDB 中的 `text-embedding-v4` 向量 + 加权 RRF；360 题 Recall@3 `98.06%`、MRR@3 `91.81%`。
- 冻结测试集子集（180题）：Recall@3 `97.78%`、MRR@3 `91.39%`。
- 三个领域：Product Recall@3 `100%` / MRR@3 `93.19%`；Process `94.17%` / `85.56%`；Policy `100%` / `96.67%`。
- 60 条 draft/inactive/expired/future/restricted 控制记录无任何候选泄漏。
- 标准 MCP Streamable HTTP 真机冒烟：协议 `2026-07-28`；三个工具均可发现，`product_search` 返回3条混合检索证据且 `is_error=false`。

## 质量门禁与失败留痕

首次等权 RRF 评测为 Recall@3 `95.56%`、MRR@3 `81.02%`，MRR 未达到预设 `85%` 门槛，脚本按失败退出。
原因是同一产品下多个高度相似段落被语义相似度互相打乱。修复采用结构化 topic 同义词增强 BM25，并将关键词排名权重设为1.5、语义排名权重设为1.0；
第二轮达到门槛。查询向量缓存位于 `evaluation/cache/` 且被 Git 忽略，后续调参可复用，避免重复付费。

正式报告：`evaluation/reports/retrieval-v2.json`。关键词基线：`evaluation/reports/retrieval-v2-keyword.json`。

## 实现边界

- 当前180条有效知识已写入 ChromaDB 并通过 `query_embeddings` 进行向量召回；PostgreSQL负责文档治理和有效记录过滤。
- MCP Server 仅绑定 `127.0.0.1:8002`，三个工具均为只读检索，不执行开户、转账、审批或任何真实银行操作。
- `.env` 默认 `ENABLE_PAID_MODEL_CALLS=false`。关闭时检索明确返回 `mode=keyword`、`degraded=true` 和原因，不冒充混合检索。
- 文档发布会产生 Embedding 调用，必须显式开启付费开关；导入草稿和解析预览不会自动发布。
- 扫描 PDF/OCR、旧版 DOC、宏、复杂嵌入对象、外部开源数据下载仍不在本阶段范围。
- `/ready` 仍返回503，因为聊天主链路、意图融合、领域 Agent、聚合/合规检查、前端和回答评测尚未完成。

## 本阶段真实模型调用计数

- 正式知识建库：18 次 Embedding API 请求。
- 第一轮冻结评测：36 次，结果未通过 MRR 门槛。
- 第二轮冻结评测：36 次，结果通过。
- MCP 混合检索真机冒烟：1 次。
- 原 RAG/MCP 阶段合计91次 Embedding API 请求；ChromaDB 切换后新增1次 MCP 混合检索冒烟，总计92次。
  向量迁移与360题复评均复用已有向量，新增调用为0。Key 始终只由服务器受保护的 `.env` 读取，未输出到日志、报告或 Git。
