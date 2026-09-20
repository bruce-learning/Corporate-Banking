# 远程基础阶段验证报告

日期：2026-09-18。结论：基础阶段通过；完整银行咨询系统尚未实现，不可用于业务验收。

## 已实际验证

- 通过指定 id_rsa 的 BatchMode SSH 成功登录，不依赖交互密码。
- 服务器：Ubuntu 22.04.1、x86_64；Python 3.10.8 位于 /root/miniconda3/bin/python。
- /root/autodl-tmp 当前挂载 /dev/nvme0n1，容量约 50 GB，建立项目之前约 50 GB 可用。
- 新项目目录：/root/autodl-tmp/project-python；创建前不存在，没有覆盖其他项目。
- 独立 .venv 约 52 MB，未更改 miniconda 基础环境或安装系统级服务。
- 安装发现默认 HTTP 包源后，显式通过 HTTPS 包源、禁用缓存、完整重装并复测。
- 精确实装依赖保存在 requirements.lock.txt；pip check 无依赖冲突。
- 远程 pytest：10 passed，2 warnings。
- 远程真实进程冒烟：/health 返回 200，/ready 如预期返回 503；临时进程已停止。
- Git 基础提交：b2f9014；仅服务器本地仓库，未推送外部平台。
- 无阿里云调用、无模型下载、无前端常驻进程或公网应用端口配置。

## 测试范围

存活/未就绪区分、未实现聊天不伪成功、默认关闭付费调用、秘密序列化隐藏、
禁止公网绑定、模型端点 HTTPS 校验、生产模式隐藏接口文档、领域与执行模式独立。

这不是意图准确率、RAG 召回率、回答合格率或 LLM-as-Judge 报告。

## 已知问题与未完成

- 测试框架提示 httpx TestClient 及 anyio 别名弃用，共 2 个警告；非失败，后续需迁移并回归。
- 鉴权、会话/数据库、Redis、Chroma、MCP、ETL、意图融合、Agent、前端和评测未完成。
- 待实现模块目录目前为空；仅有目录不表示具备对应功能，Git 不跟踪空目录。
- 当前仅 root SSH 开发；公开部署前需进一步处理运行账户、TLS、鉴权与运维。
- 没有创建真实 .env，也没有读取或上传本机阿里云 Key。

## 复现

```bash
cd /root/autodl-tmp/project-python
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
.venv/bin/python scripts/smoke_test.py
```

后续重建环境应从精确锁定文件安装，并显式使用 HTTPS 包源：

```bash
.venv/bin/python -m pip --isolated install --index-url https://mirrors.aliyun.com/pypi/simple -r requirements.lock.txt
```
