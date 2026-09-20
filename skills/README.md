# 银行对公 Skills

服务启动时加载 `skills/**/SKILL.md`。`agents` 限制适用 Agent，`keywords` 命中当前问题时注入；
关键词为空的 Skill 是全局规范。修改后由管理员调用 `POST /api/v1/admin/skills/reload` 热加载。

Skill 只补充业务规范，不能覆盖代码中的安全边界或扩大工具权限。
