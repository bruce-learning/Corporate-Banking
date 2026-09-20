# ChromaDB 向量后端迁移验证报告

验证日期：2026-09-19

验证位置：`/root/autodl-tmp/project-python`

## 迁移结果

- ChromaDB 版本：`1.5.9`。
- 服务地址：`127.0.0.1:8001`，不对公网监听。
- 持久化目录：`var/chroma`。
- 集合：`bank_knowledge_v1`，余弦空间。
- 从 PostgreSQL 读取并迁移180条既有1024维阿里云向量，Chroma 集合计数180。
- 迁移脚本采用幂等 `upsert`，迁移过程模型调用0次。
- Chroma 记录同时保存知识正文和扁平检索元数据：文档ID、领域、产品、状态、受众、有效期、合成标记和模型名称。

## 检索与协议验证

- 360条冻结题复用既有查询向量，通过 Chroma `query_embeddings` 检索。
- Recall@3：`98.06%`；MRR@3：`91.81%`；60条过滤负例零泄漏。
- MCP Streamable HTTP 真机调用返回 `mode=hybrid`、`vector_backend=chromadb`、3条证据且无错误。
- 默认关闭付费调用时，MCP 明确返回 `mode=keyword`、`vector_backend=none`，不冒充向量检索。
- 远程完整测试：`47 passed`，包含有效期/受众过滤、发布写入 Chroma、PostgreSQL 不保存新向量及文档停用删除向量测试。

## 数据职责与边界

- PostgreSQL 是文档版本、状态、权限与审计事实源；ChromaDB 是实际语义向量检索后端。
- 旧 PostgreSQL `embedding_json` 数据暂作为一次性迁移/回滚来源保留，但检索代码不再读取；新发布文档的向量只写入 ChromaDB。
- 文档发布先生成阿里云向量并写入 Chroma，再把 PostgreSQL 文档置为已发布；停用时 PostgreSQL 禁用并删除对应 Chroma 记录。
- API 和 MCP 都连接同一个 Chroma HTTP 服务，避免两个进程分别打开持久化目录。
- Chroma、API、MCP、Redis、PostgreSQL 均仅绑定本机或项目内部地址；公开部署仍需 TLS、专用运行账户和网络访问控制。
