# 企业级银行对公智能客服 Agent 系统

## 项目简介

本项目是一个面向银行对公业务场景的可运行 AI Agent 客服系统，基于 **FastAPI + Alibaba Cloud Qwen + MCP + ChromaDB + Redis + PostgreSQL** 构建。

系统不是单轮大模型聊天封装，而是一条完整、可审计的业务问答链路：对用户问题进行风险检查和三路意图识别，根据问题复杂度选择单 Agent、并行 Agent 或 Plan-and-Execute 模式，再通过标准 MCP 工具完成知识检索、答案生成、引用校验、降级处理、会话记忆和质量评测。

当前仓库包含后端服务、前端演示工作台、虚构银行演示知识、合成评测集、文档导入流水线、Docker Compose 和完整测试。它用于工程演示与学习，**不是银行生产系统，不连接真实账户，也不执行交易、审批或资金操作**。

## 核心能力

- **多 Agent 业务路由**：Product、Process、Policy 三个领域 Agent 分别处理产品、办理流程和收费/合规问题。
- **受控 ReAct Agent**：领域 Agent 可根据已有证据选择继续补查或结束，限制最大步骤、工具次数、执行时间和证据数量；重复检索、越界或超时会显式降级。
- **三种外层执行模式**：简单问题走单 Agent；跨领域问题并行执行；复杂任务使用受控 Plan-and-Execute。
- **三路意图融合**：Pattern、Embedding 和 LLM Few-shot 结合动态权重与低置信度降级。
- **标准 MCP 工具调用**：使用 MCP Python SDK v2 和 Streamable HTTP，包含参数校验、超时、重试、缓存、熔断和只读回退。
- **混合 RAG 检索**：ChromaDB 向量检索、BM25 关键词检索和加权 RRF 融合，并进行领域、受众、状态及有效期过滤。
- **动态 Skills 注入**：每个 Agent 加载自己的业务处理规范，全局安全 Skill 对所有 Agent 生效。
- **三层记忆体系**：Redis 保存当前会话工作记忆；ChromaDB 保存历史会话摘要和用户画像。
- **文档解析与知识发布**：支持 JSON、JSONL、TXT、Markdown、DOCX、PDF、XLSX、XLS 和受限扫描 PDF OCR。
- **后台任务**：Redis Streams 驱动文档导入、OCR、长期记忆生成和失败重试，PostgreSQL 保存任务真相。
- **可观测与评测**：记录意图、Agent、计划、工具、证据、引用、降级、模型用量和耗时，并提供评测运行与 Bad Case 闭环。
- **前端演示**：提供登录、会话、聊天、答案追踪、记忆管理、Skills、后台任务和文档导入界面。

## 系统架构

```mermaid
flowchart TD
    UI[前端演示工作台] --> API[FastAPI 接入层]
    API --> AUTH[认证 / 限流 / 会话锁]
    AUTH --> SAFE[风险检查]
    SAFE --> INTENT[Pattern + Embedding + LLM 意图融合]
    INTENT --> ROUTER[任务路由与执行模式]
    ROUTER --> PRODUCT[Product ReAct Agent]
    ROUTER --> PROCESS[Process ReAct Agent]
    ROUTER --> POLICY[Policy ReAct Agent]
    PRODUCT --> MCP[MCP 工具网关]
    PROCESS --> MCP
    POLICY --> MCP
    MCP --> MCP_SERVER[银行 MCP Server]
    MCP_SERVER --> RAG[ChromaDB + BM25 + RRF]
    RAG --> PG[(PostgreSQL)]
    RAG --> CHROMA[(ChromaDB)]
    API --> REDIS[(Redis 工作记忆 / 缓存 / Streams)]
    REDIS --> WORKER[异步 Worker]
    WORKER --> PG
    WORKER --> CHROMA
    ROUTER --> QWEN[Alibaba Cloud Qwen]
    QWEN --> GUARD[证据聚合 / 引用检查 / 降级]
    GUARD --> UI
```

### 领域 Agent 与执行模式

| 维度 | 说明 |
|---|---|
| Product Agent | 产品功能、适用场景、产品条件和能力边界 |
| Process Agent | 办理材料、前置条件、步骤、渠道、时效和收费环节 |
| Policy Agent | 收费标准、额度、安全、合规与政策边界 |
| ReAct | 每个领域内部执行有界的 Action → Observation → 补查/结束循环 |
| Fixed | 单一领域的简单问题 |
| Parallel | 多领域问题并行检索和生成 |
| Plan-and-Execute | 有依赖关系的复杂问题按受控计划执行 |

## 技术栈

| 类别 | 技术 |
|---|---|
| Web 与 API | FastAPI、Uvicorn、Pydantic v2、pydantic-settings |
| LLM | Alibaba Cloud `qwen-plus`，OpenAI Compatible API |
| Embedding | Alibaba Cloud `text-embedding-v4`，1024 维 |
| Agent | 有界 ReAct、领域 Agent、并行编排、Plan-and-Execute、动态 Skills |
| 工具协议 | MCP Python SDK v2、Streamable HTTP |
| RAG | ChromaDB、BM25、加权 RRF、查询改写、引用检查 |
| 工作记忆与队列 | Redis、Redis Streams |
| 数据库 | PostgreSQL、SQLAlchemy Async、Alembic |
| 文档处理 | python-docx、pypdf、openpyxl、xlrd、Tesseract OCR |
| 可观测性 | 请求追踪、模型用量、Prometheus 文本指标、Bad Case |
| 部署 | Docker、Docker Compose、原生 Linux 进程模式 |
| 测试 | pytest、pytest-asyncio、HTTP/MCP/故障恢复冒烟测试 |

## 快速开始

### Docker Compose

运行环境建议：Linux、Docker Engine、Docker Compose v2、4 核 CPU、8 GB 内存和 40 GB 以上磁盘。

1. 准备配置：

```bash
cp .env.docker.example .env.docker
chmod 600 .env.docker
mkdir -p var/secrets
openssl rand -base64 24 > var/secrets/docker-admin-initial-password
chmod 600 var/secrets/docker-admin-initial-password
```

2. 编辑 `.env.docker`，至少配置：

```dotenv
ENABLE_PAID_MODEL_CALLS=true
DASHSCOPE_API_KEY=your_api_key
REACT_ENABLED=true
REACT_MAX_STEPS=3
REACT_MAX_TOOL_CALLS=3
REACT_TIMEOUT_SECONDS=15
POSTGRES_PASSWORD=replace_with_random_hex
DATABASE_URL=postgresql+asyncpg://bankapp:replace_with_random_hex@postgres:5432/bank
REDIS_PASSWORD=replace_with_random_hex
REDIS_URL=redis://:replace_with_random_hex@redis:6379/0
```

真实 API Key、数据库密码和管理员密码不得提交 Git。

3. 构建并启动：

```bash
docker compose --env-file .env.docker config --quiet
docker compose --env-file .env.docker build
docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker ps
```

4. 验证：

```bash
curl -fsS http://127.0.0.1:18000/health
curl -fsS http://127.0.0.1:18000/ready
docker compose --env-file .env.docker --profile tools run --rm runtime-check
```

访问地址：

- 前端：<http://127.0.0.1:18000/>
- 健康检查：<http://127.0.0.1:18000/health>
- 就绪检查：<http://127.0.0.1:18000/ready>

初始管理员用户名为 `admin`，密码保存在 `var/secrets/docker-admin-initial-password`。

### 初始化虚构演示知识

如需加载仓库自带的虚构银行演示知识：

```bash
docker compose --env-file .env.docker --profile bootstrap run --rm bootstrap-knowledge
```

该命令会调用阿里云 Embedding API。它只用于演示数据，不会下载真实银行数据集。

### 本地 Python 开发

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python scripts/run_api.py
```

完整后端还需要 PostgreSQL、Redis、ChromaDB、MCP 和 Worker。建议优先使用 Docker Compose 启动全栈依赖。

## 真实数据集接入

本仓库只包含虚构演示知识和合成评测数据，**不包含任何真实银行业务数据集**。

使用具体实际数据前，部署者需要自行完成：

1. 获得数据使用授权；
2. 去除客户姓名、账号、手机号、证件号等敏感信息；
3. 使用管理员页面或 `/api/v1/admin/ingestion-jobs` 上传文件；
4. 指定 `product`、`process` 或 `policy` 领域；
5. 检查解析结果、分块、有效期、受众范围和引用内容；
6. 管理员确认发布后写入 PostgreSQL 和 ChromaDB。

上传流程：

```text
文件上传 → 格式与大小校验 → 解析/OCR → 分块 → 草稿预览
        → 人工审核 → Embedding → ChromaDB → 发布后参与检索
```

数据真实性、合法授权、脱敏、更新频率和失效处理由部署者负责。

## 目录结构

```text
Corporate-Banking-AI-Agent/
├── app/
│   ├── main.py                    # FastAPI 应用入口
│   ├── config.py                  # 环境与安全配置
│   ├── api/routes/                # 认证、会话、聊天、文档、评测、记忆
│   ├── core/
│   │   ├── agents/                # Product / Process / Policy Agent
│   │   ├── application/           # 编排、知识、任务与业务服务
│   │   ├── intent/                # 三路意图识别
│   │   ├── memory/                # 三层记忆体系
│   │   ├── rag/                   # BM25、向量检索与融合
│   │   └── skills/                # 动态 Skills 加载
│   ├── infrastructure/
│   │   ├── cache/                 # Redis
│   │   ├── database/              # PostgreSQL
│   │   ├── llm/                   # 阿里云模型客户端
│   │   ├── mcp/                   # 标准 MCP 网关
│   │   ├── queue/                 # Redis Streams
│   │   └── vector/                # ChromaDB
│   ├── etl/                       # DOCX/PDF/Excel/OCR 解析与分块
│   └── models/                    # schemas、entities、enums
├── frontend/                      # 无构建依赖的前端演示工作台
├── mcp_server/                    # 银行 MCP Server
├── skills/                        # Agent 业务规范
├── migrations/                    # Alembic 迁移
├── datasets/                      # 虚构知识与合成评测集
├── evaluation/                    # 评测报告与人工盲评包
├── scripts/                       # 启动、初始化、评测、冒烟和运维脚本
├── tests/                         # 自动化测试
├── docs/                          # 工程与部署文档
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── requirements.txt
├── requirements.lock.txt
└── README.md
```

## 相关文档

- `docs/docker-deployment.md`：Docker Compose 部署
- `docs/engineering-guide.md`：工程与接口说明
- `docs/backend-quality-verification.md`：后端质量验证
- `docs/skills-memory-verification.md`：Skills 与记忆验证
- `docs/memory-worker-frontend-verification.md`：Worker、记忆治理与前端验收

## License

本项目采用 MIT License，详见 `LICENSE`。
