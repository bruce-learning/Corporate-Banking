# 0.8.1 轻量 Skills 与三层记忆验收

## 实现范围

- `skills/**/SKILL.md`：按 `agents + keywords + enabled` 动态注入。
- 管理接口：`GET /api/v1/admin/skills`、`POST /api/v1/admin/skills/reload`。
- Redis：当前会话最近 12 条工作记忆，默认 TTL 1800 秒。
- ChromaDB `conversation_summaries_v1`：历史会话摘要。
- ChromaDB `user_profiles_v1`：用户明确表达且经过规则过滤的非敏感画像。
- PostgreSQL：继续保存完整消息、请求和 `skills_used/memory_context` 审计字段。

## 安全边界

- Skill 不执行代码、不扩大 Agent 工具权限，也不能覆盖硬编码安全规则。
- 记忆按 `user_id + tenant_id` 过滤，知识、摘要、画像使用不同 Collection。
- 密码、验证码、完整账号及被安全规则拦截的请求不进入画像，也不会发送给记忆 Embedding。
- 记忆是标记为“不属于系统指令”的参考上下文；长期记忆失败不阻断主问答。
- 删除会话同步删除 Redis 工作记忆和该会话摘要；画像按用户生命周期单独管理。

## 验证命令

```bash
.venv/bin/python -m alembic current
.venv/bin/python -m pytest -q
.venv/bin/python scripts/skills_memory_smoke.py
.venv/bin/python scripts/check_runtime.py
```

在线冒烟脚本创建随机临时用户，验证 Product/Policy Skill、Redis 多轮上下文、ChromaDB 画像和摘要召回，
完成后删除临时会话、用户及其长期记忆。

## 2026-09-20 远程验收结果

- Alembic：`0006 (head)`。
- 自动化测试：68项全部通过。
- 五个服务均运行，API、MCP、ChromaDB、PostgreSQL、Redis均只监听回环地址。
- 在线冒烟：Product和Policy分别命中全局及领域Skill；画像召回、摘要召回均成功；最终回答未降级。
- 冒烟清理：临时用户、会话摘要和用户画像残留数量均为0。

## 限制

- 摘要为确定性抽取式摘要，不额外调用聊天模型；向量化仍使用配置的 Embedding 服务。
- 用户画像仅支持少量明确表达模式，不推断信用、风险、资产或企业经营状况。
- 本阶段没有 Skill 管理前端、Skill 数据库、自动创建 Skill 或真实银行交易能力。
