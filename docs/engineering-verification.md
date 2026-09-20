# 工程底座阶段验收报告

日期：2026-09-18。版本：0.2.0-engineering。
结论：本阶段的鉴权、会话与原生基础依赖已完成并在指定服务器验证。
**这不是完整银行咨询 MVP、增强版或生产系统验收。**

## 远程实现

- 项目 /root/autodl-tmp/project-python，Ubuntu 22.04.1、现有 Python 3.10.8 独立虚拟环境。
- FastAPI 登录、退出、当前身份、普通/管理员权限依赖；没有自助注册。
- Argon2id 密码哈希、高熵随机访问令牌、SHA-256 摘要存储、3600 秒有效期、数据库撤销核验。
- PostgreSQL 保存用户、凭证、会话、消息和请求记录，Redis 保存短期上下文/限频/锁。
- 0001 初始迁移 + 0002 请求关联迁移；HTTP X-Request-ID 可关联成功写入请求记录。
- 用户只能访问自己的会话，管理员没有默认读取他人会话的权限。
- 内部消息写入、序号、缓存重建、删除级联及分页边界；不开放伪造助手消息接口。
- 模型契约、工具调用字段、用量/错误分类及显式 fake 客户端，仅用于测试。
- 原生实例 bootstrap/start/stop/status、用户初始化和常驻 API 管理脚本。

## 实际验证结果

| 检查 | 结果 |
|---|---|
| 远程 pytest | 33 passed，0 failed，0 skipped，无警告，最终一次约 6.18 秒 |
| 原基础测试 | 保留原 10 项检查目标；阶段标识更新为 engineering，换用 HTTPX ASGITransport，弃用警告消除 |
| pip check | 无依赖冲突；精确版本保存在 requirements.lock.txt |
| 真实 API 重启 | 原令牌、会话和消息可继续查询 |
| 真实 Redis 中断/恢复 | 登录和需要 Redis 保护的删除返回 503；恢复后缓存可从 PostgreSQL 重建 |
| 真实 PostgreSQL 中断/恢复 | 存活检查仍 200，依赖及存储操作 503；恢复后消息仍存在 |
| 真实 TCP 删除 | 删除成功 204，再查询 404 |
| 常驻实例 | API / Redis / PostgreSQL 均运行 |
| 监听 | 8000 / 16379 / 15432 均为 127.0.0.1，没有配置公网暴露 |
| 新秘密文件 | .env 与 admin 初始密码为 600；postgres/redis 账户不能读取 |
| 测试清理 | bank_test 用户数最终为 0；业务库只保留初始化的 admin 用户 |
| 付费调用 | 0；ENABLE_PAID_MODEL_CALLS=false，无真实 Aliyun/Embedding/Judge 调用 |

原始证据见 verification-evidence/engineering-tests.xml 和 lifecycle-result.json。
这些是功能/安全/故障测试，不是业务问答准确率、RAG 召回率或 LLM-as-Judge 成绩。

## 实施中发现并处理的问题

1. Redis 退出后 TIME_WAIT 被误报为其他实例占用：检查使用 SO_REUSEADDR，
   同时新增真实活动监听端口拒绝测试；真实重启复测通过，没有强杀未知进程。
2. 服务器预设 APT 包源含 proposed：新安装的两个 PostgreSQL common 管理包显式改为 jammy/main 238，
   PostgreSQL 14.24 与 Redis 6.0.16 的 Ubuntu 包来自相应源；没有修改全局包源。
3. 新增请求关联使用独立 0002 迁移，不修改已应用的 0001；迁移前暂停 API 并备份业务库。
4. 权限脚本最初要求既有 SSH 目录必须 700，过于严格：改成校验非 owner 不可写，
   保留既有 SSH 配置，不读取或修改密钥内容。新增秘密文件仍严格为 600。
5. 初次失败的生命周期检查遗留一条自建测试用户：核对 UUID 和用户名后仅删除该 bank_test 样本，
   后续检查自行清理，未删除业务用户或业务数据。

## 数据、权限与进程

- PostgreSQL / Redis 使用非 root 系统账户，仅对 /root 等父目录给予各账户穿越 ACL，
  没有给予目录列举权限，也没有通过 chmod 755 公开整个 /root。
- 项目代码文件权限收紧，数据库账户不拥有应用源码写入权限。
- 实际持久化盘约 50 GB；本阶段盘使用约 152 MB，其中 .venv 约 93 MB、项目 PostgreSQL 约 59 MB。
- .env、var、密码和数据库备份均被 Git 排除，也未同步到本机。
- admin 初始密码位于 var/secrets/admin-initial-password，只能由用户在自己的终端查看。
- 包安装生成的系统默认 14/main cluster 未启动；实际项目使用独立 var/postgresql，不使用默认 cluster。
- 当前仅本机开发进程运行，不注册开机启动，不承诺实例停止后自动恢复进程。

## 明确未完成

Chroma / ETL / 三路意图 / Product-Process-Policy Agent / 真实 MCP / Vue / 数据集 / 业务评测均待完成。
真实阿里云客户端尚未接入，fake 向量不得作为真实语义检索。
自动数据保留清理、异地备份与完整备份恢复演练尚未完成。
Redis 宿主机 overcommit_memory=0 警告未通过更改宿主机配置处理，内存压力风险需后续运维评估。
API 目前仍为 root 开发进程；当前 30 秒锁只覆盖短时操作，尚不是长时 Agent 全流程锁。
公网 IP/设备风控、TLS、专用应用运行账户与生产维护等级不在本阶段完成声明内。

因此 /health=200 表示存活，/api/v1/health/dependencies=200 表示基础依赖就绪，
/ready 和 /api/v1/health/ready 仍为 503，不能宣称银行客服已上线。

## 复现与继续开发

```bash
cd /root/autodl-tmp/project-python
.venv/bin/python scripts/native.py status
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_runtime.py
```

lifecycle_test.py 会短暂中断项目依赖，仅在独占开发维护窗口执行。
后续从统一阿里云客户端、合成知识/版本发布、混合检索与真实 MCP 开始。
在线模型调用前需在服务器配置密钥并确认调用预算，不将本机秘密自动上传。
