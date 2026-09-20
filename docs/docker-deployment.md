# Docker Compose 部署说明

本部署使用全新的 PostgreSQL、Redis、ChromaDB 和应用数据卷，不迁移裸机数据。Docker 版与裸机版可以使用不同端口并行验证。

## 服务组成

默认启动 `postgres`、`redis`、`chroma`、`migrate`、`init-admin`、`mcp`、`worker` 和 `api`。只有 API 映射到宿主机回环地址，数据库、缓存、向量库和 MCP 不发布宿主机端口。

## 准备配置

```bash
cd /root/autodl-tmp/project-python
.venv/bin/python scripts/create_docker_env.py
```

脚本读取服务器现有 `.env` 中的阿里云配置，为 Docker PostgreSQL、Redis 和管理员生成独立随机密码；不会输出任何秘密，也不会覆盖已经存在的 Docker 配置。阿里云 Key 只写入服务器 `.env.docker`，不得写入镜像、Compose 文件或 Git。

## 构建与启动

```bash
docker compose -f docker-compose.seetacloud.yml --env-file .env.docker config --quiet
docker compose -f docker-compose.seetacloud.yml --env-file .env.docker build
docker compose -f docker-compose.seetacloud.yml --env-file .env.docker up -d
docker compose -f docker-compose.seetacloud.yml --env-file .env.docker ps
```

初始管理员用户名为 `admin`，密码保存在服务器 `var/secrets/docker-admin-initial-password`，不要粘贴到聊天或提交 Git。

## 初始化虚构演示知识

只有需要项目自带的虚构演示知识时才执行：

```bash
docker compose -f docker-compose.seetacloud.yml --env-file .env.docker --profile bootstrap run --rm bootstrap-knowledge
```

该命令会调用阿里云 Embedding，因此必须确认 `.env.docker` 中已显式启用付费调用。

## 真实数据集边界

仓库不提供、下载或声称包含真实银行业务数据。接入真实数据时，使用管理员页面或 `/api/v1/admin/ingestion-jobs` 上传经过授权和脱敏的文件，并选择 `product`、`process` 或 `policy` 领域。系统支持 JSON、JSONL、TXT、Markdown、DOCX、文本型 PDF、XLSX、XLS，以及受限的扫描 PDF OCR。

上传文件先进入解析、分块和草稿状态。必须检查预览、字段、有效期、受众范围和敏感信息，再由管理员发布；发布后才会写入 ChromaDB 并参与检索。数据来源、授权、脱敏、正确性和更新责任由部署者承担。

## 验证

```bash
curl -fsS http://127.0.0.1:18000/health
curl -fsS http://127.0.0.1:18000/ready
docker compose -f docker-compose.seetacloud.yml --env-file .env.docker --profile tools run --rm runtime-check
docker compose -f docker-compose.seetacloud.yml --env-file .env.docker logs --tail=100 api mcp worker
```

本地 Windows 访问可使用 SSH 隧道：

```powershell
& "C:\Windows\System32\OpenSSH\ssh.exe" -p 26109 -N -L 18000:127.0.0.1:18000 root@connect.nmb2.seetacloud.com
```

浏览器访问 `http://127.0.0.1:18000/`。

## 停止与删除

```bash
docker compose -f docker-compose.seetacloud.yml --env-file .env.docker down
```

上面的命令保留数据卷。只有明确确认要永久删除 Docker 版的全部数据库、缓存、向量和上传数据时，才可以使用 `down -v`。命名卷不是备份，生产使用前仍需建立 PostgreSQL、Redis、ChromaDB 和上传文件的独立备份方案。

当前 SeetaCloud 实例本身位于缺少 `NET_ADMIN` 权限的受限容器中，Docker 不能创建 NAT bridge。因此 `docker-compose.seetacloud.yml` 使用 host 网络和独立回环端口：API `18000`、PostgreSQL `25432`、Redis `26379`、ChromaDB `28001`、MCP `28002`。Docker 使用持久化盘上的 `/root/autodl-tmp/docker-data-vfs` 和 `vfs` 驱动；这些服务都绑定 `127.0.0.1`，不会直接公开。普通具备 Docker bridge 能力的 Linux 主机可改用标准 `docker-compose.yml` 和 `.env.docker.example`。

该实例没有 systemd。实例重启后如 Docker 未自动运行，执行 `bash deploy/docker/start-daemon.sh`，再执行 Compose 启动命令。

当前实例的宿主平台还会在 Docker 解包镜像层时拒绝 `unshare` 操作。这代表嵌套容器权限未开放，不是 Dockerfile 或 Compose 错误。在平台开放所需权限之前，本实例只能保存和验证 Docker 打包文件，不能真正构建或启动容器。若更换为支持 Docker 的实例，继续使用本文件命令即可；SeetaCloud 专用 Compose 已使用可访问的镜像代理地址。

## 限制

- 这是单机 Compose 部署，不是高可用集群。
- Docker 化不改变模型效果，也不会提高现有评测指标。
- 不接入真实银行核心系统，不执行交易或审批。
- 公网开放前仍需增加反向代理、TLS、域名、防火墙、密码轮换和备份恢复演练。
