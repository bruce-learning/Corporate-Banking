# 工程底座：接口与运维

## 运行方式

项目位于 /root/autodl-tmp/project-python，API 127.0.0.1:8000，
PostgreSQL 127.0.0.1:15432，Redis 127.0.0.1:16379，均不开放公网。
PostgreSQL 和 Redis 分别使用 postgres/redis 系统账户，API 当前仍为 root 开发进程。
公开部署前必须补齐独立应用运行账户、TLS、进一步限流及运维配置。

```bash
cd /root/autodl-tmp/project-python
.venv/bin/python scripts/native.py status
.venv/bin/python scripts/native.py start
.venv/bin/python scripts/native.py stop
```

不要再次用 `.env.example` 覆盖已有 `.env`。若示例占位符误覆盖了项目专属 PostgreSQL/Redis 凭据，使用：

```bash
.venv/bin/python scripts/native.py rotate-storage-credentials
```

该动作仅轮换存储凭据并保留模型 Key 等其他配置，不输出新密码。

仅管理本项目实例，stop 不删除数据。无 systemd/自动重启/开机自启承诺。
分别启动/停止可用 start-api/stop-api、start-pg/stop-pg、start-redis/stop-redis。

## 配置与账户

.env 为 600 权限，仅 root 可读；数据库/Redis 随机密码不会打印。
管理员 admin 初始随机密码保存在 var/secrets/admin-initial-password，文件权限 600。
创建普通用户或管理员：

```bash
.venv/bin/python scripts/native.py create-user demo user
.venv/bin/python scripts/native.py create-user secondadmin admin
```

密码通过终端隐藏输入，12～128 字符；不传命令行。已有同名账户不覆盖。
尚无用户管理页面、自助注册和重置密码接口。
不要上传 .env、var、SSH 私钥，日志不得记录请求正文/凭证。

## API

| 接口 | 权限/行为 |
|---|---|
| POST /api/v1/auth/login | JSON username/password，返回临时 bearer token |
| POST /api/v1/auth/logout | 撤销当前 token，成功 204 |
| GET /api/v1/auth/me | 当前身份，不返回密码哈希 |
| POST /api/v1/conversations | 身份由服务端决定；JSON title，可省略 |
| GET /api/v1/conversations | 仅自己的列表，offset/limit 分页 |
| GET /api/v1/conversations/{id} | 仅自己的会话 |
| GET /api/v1/conversations/{id}/messages | 仅自己的消息，after_sequence/limit 分页 |
| DELETE /api/v1/conversations/{id} | 删除自己的会话、消息、关联记录和上下文缓存 |
| GET /api/v1/admin/status | 仅管理员，依赖状态，不授权查看别人的对话 |
| GET /health 或 /api/v1/health/live | 进程存活，200 不表示银行业务完成 |
| GET /api/v1/health/dependencies | 数据库迁移结构和 Redis 都就绪才 200 |
| GET /ready 或 /api/v1/health/ready | 银行主链路未完成，仍返回 503 |

后续请求使用 Authorization: Bearer <token>。token 不放 URL，不持久化在前端代码。
401 表示未认证/过期/撤销；403 表示权限不足；他人的会话统一 404，避免暴露存在性；
409 会话冲突；413 请求体超限；422 字段不合法（不会返回输入密码）；429 登录限频；503 依赖异常。
全体 HTTP 请求包含服务端生成的 X-Request-ID。成功登录、退出、会话创建和内部消息写入
存储对应 RequestRecord；失败/读取请求目前通过不含正文的请求日志记录，不承诺所有请求都写入数据库。

## 当前参数与边界

- token 有效期 3600 秒；只存 SHA-256 摘要，退出立即在数据库撤销，每次请求核验。
- 密码 Argon2id（19 MiB / t=2 / p=1），不自行实现密码哈希。
- 登录同用户名 5 次失败/900 秒，成功不计入失败；暂未实现公网 IP/设备风控。
- 请求体上限 64 KiB，包括无 Content-Length 的流式输入；标题 120 字符，列表最多 100 条。
- 近期上下文最多 12 条消息，TTL 1800 秒，按身份/会话隔离，从数据库重建。
- 锁租期 30 秒，用于当前有界 CRUD/内部消息操作；并非已支持长时 Agent 任务锁续租。
  接入聊天主链路前必须扩展全任务并发控制，不能直接拿这个租期覆盖无限模型执行。
- Redis 设置 128 MB 上限及 noeviction，避免锁被淘汰；存储不足明确失败。
- 数据库 schema 0002（0001 初始结构 + 0002 请求关联）；不是启动时 create_all。
- 对话/追踪的自动 7 天保留清理尚未启用；当前提供显式删除，不声称已完成自动数据生命周期。
- 删除只覆盖活动存储；既有备份中的数据不随删除自动消失。
- 已保留迁移前业务库备份 var/postgresql/pre-0002.dump；尚无异地备份、自动过期或完整恢复演练。
- Redis 日志提示宿主机 overcommit_memory=0；未改宿主机内核配置，内存压力下 AOF 重写风险待运维处理。
- 假模型 backend=fake，仅用于接口测试，不能作为语义向量、银行回答或 Judge 验收。
- 尚不提供聊天、前端、知识管理和评测业务接口。

## 验证与来源

```bash
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
.venv/bin/python scripts/smoke_test.py
.venv/bin/python scripts/check_runtime.py
```

lifecycle_test.py 会暂时停止项目数据库/Redis并恢复，仅应在独占开发维护窗口执行。
测试只使用 bank_test 和专属键前缀，禁止全局 FLUSHDB/FLUSHALL；不并行运行维护检查。

实现参考：[SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)、
[Alembic asyncio 迁移](https://alembic.sqlalchemy.org/en/latest/cookbook.html)、
[Redis Python 客户端](https://redis.io/docs/latest/develop/clients/redis-py/)、
[OWASP 密码存储](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)。
