# 知识基础阶段验证报告

验证日期：2026-09-19

验证位置：远程持久化目录 `/root/autodl-tmp/project-python`

## 已验证结果

- 远程完整测试：`40 passed in 6.93s`。
- 阿里云真实聊天：后端 `aliyun`，模型 `qwen-plus`，有非空输出，本次合计 15 tokens。
- 阿里云真实向量：模型 `text-embedding-v4`，2条文档向量和1条查询向量均返回1024维。
- `.env` 默认 `ENABLE_PAID_MODEL_CALLS=false`；真实冒烟仅通过单次命令临时开启。
- 合成知识：180条，Product/Process/Policy 各60条。
- 过滤负例：60条，覆盖 draft/inactive/expired/future/restricted。
- 检索评测：360条，开发90、验证90、冻结测试180；产品家族不跨集合。
- 文档解析单测覆盖 DOCX 段落/表格、XLSX Sheet/行、UTF-8 文本、损坏文件和无文本 PDF 的 `ocr_required`。
- 管理员解析预览接口经过真实鉴权测试；普通用户返回403，管理员返回解析元数据和受限预览。

## 数据文件哈希

| 文件 | 行数 | SHA-256 |
|---|---:|---|
| `knowledge.jsonl` | 180 | `92c4218cff5f7675de21afbf04f68f0c864bde625426028f3515ba49d1747567` |
| `knowledge_controls.jsonl` | 60 | `d7a6dfaecd5153373d4e8a95a890dd30e1a1bd4be44de33ede39e790e238b5b7` |
| `retrieval_eval.jsonl` | 360 | `3f00e44993e603e29dc982d25b3b712447b1e92e10fb04573b5c043750918964` |

## 当前边界

- 文档接口仅解析预览，不持久化、审核、发布或建立索引。
- 文本型 PDF 可解析；扫描件不做 OCR，加密 PDF 不支持。
- Excel 不执行公式、宏和外部链接；XLS 只读取保存的公式结果。
- 外部开源数据目前只建立候选目录，尚未下载或批准，不能宣称已纳入数据集。
- `/ready` 仍返回503；聊天、混合检索、标准 MCP、领域 Agent 和前端尚未完成。
