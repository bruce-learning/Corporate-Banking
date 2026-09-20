# 0.9 异步记忆、OCR 与前端验收记录

日期：2026-09-20

版本：`0.9.0-fullstack-demo`

迁移：Alembic `0007 (head)`

## 一、交付范围

本阶段在既有银行对公智能客服后端上完成：

1. PostgreSQL 持久化后台任务，Redis Streams 仅负责低延迟唤醒。
2. 独立 Worker 执行长期记忆写入和文档导入；支持有限重试、退避和中断任务恢复。
3. Redis 工作记忆、ChromaDB 历史摘要与用户画像组成三层记忆；长期记忆不阻塞聊天响应。
4. 用户画像和摘要的查看、修改、停用、单项删除、按会话删除与全部删除。
5. 每用户聊天限流与每日记忆 Embedding 预算。
6. 扫描 PDF 的受限 OCR，并强制进入草稿预览，不自动发布。
7. 同源前端演示：认证、会话、聊天、答案追踪、记忆中心、Skills、后台任务与文档导入。

## 二、后台任务设计与故障边界

- PostgreSQL `background_jobs` 是任务状态真相源；Redis Stream 消息丢失时，Worker 仍会轮询数据库补偿。
- API 入队失败时不会把同步重活偷偷放回请求线程；聊天返回中明确记录记忆写入状态。
- Worker 启动时将超时的 `running` 任务恢复为可重试状态。
- 任务只有 `queued`、`retrying`、`running`、`completed`、`failed` 等受控状态，并受最大重试次数约束。
- 文档正文仍在 PostgreSQL/ChromaDB 的既有发布链路内，队列不绕过解析校验、权限或发布状态。

真实故障恢复验收：停止 Worker 后发起聊天，请求正常返回且长期记忆任务保持排队；重启 Worker 后任务完成并可从记忆接口读取。验收脚本完成后删除临时用户、会话和向量记忆。

## 三、OCR 约束

- 仅对扫描 PDF 开启 OCR；DOCX、XLSX/XLS、TXT/MD/JSON/JSONL 继续走结构化解析。
- 最多 30 页、200 DPI，语言为 `chi_sim+eng`；每页有超时并返回警告。
- OCR 必须由独立 Worker 执行，禁止在 API 请求中直接跑 Tesseract。
- OCR 结果可能有错字、表格错位或字段遗漏，始终保存为待审草稿，不允许自动发布。
- 图片文件、手写体、印章识别、复杂版面还原和加密 PDF 不在当前范围。

服务器原生依赖：`poppler-utils`、`tesseract-ocr`、`tesseract-ocr-chi-sim`、`tesseract-ocr-eng`。

## 四、前端说明

前端位于 `frontend/`，由 FastAPI 根路径同源提供，不依赖 Node.js、CDN 或第三方前端构建服务。

- 普通用户：登录、创建/删除会话、连续聊天、查看证据与路由追踪、管理自己的长期记忆。
- 管理员：在普通用户功能外查看 Skills、热加载 Skills、查看后台任务、上传异步解析文档。
- 动态内容使用 `textContent` 渲染，避免将模型文本直接作为 HTML 插入。
- Token 仅保存在 `sessionStorage`；这适合受控演示，不替代生产级 HttpOnly Cookie、刷新令牌和 CSRF 防护。

## 五、验收结果

- 完整自动化测试：`72 passed`。
- JavaScript 语法检查：`node --check frontend/app.js` 通过（本地执行；服务器无需 Node）。
- 真实扫描 PDF OCR：通过，识别 1 页，解析器版本 `etl-1.2`。
- 在线 Skills + 三层记忆：Product/Policy Skill 命中，画像和摘要均可回忆，最终请求未降级。
- Worker 停止/恢复：聊天在 Worker 停止时成功，恢复后排队记忆完成。
- 前端入口：根路径与静态资源自动化测试通过；窄窗口登录页人工视觉检查通过。

上述结果证明当前合成演示环境的实现与回归状态，不代表真实银行生产稳定性、OCR 准确率或业务合规认证。

## 六、访问与运行

```bash
cd /root/autodl-tmp/project-python
.venv/bin/python scripts/native.py start
.venv/bin/python scripts/native.py status
.venv/bin/python scripts/check_runtime.py
```

Windows 本机建立 SSH 隧道：

```powershell
& "C:\Windows\System32\OpenSSH\ssh.exe" -i "C:\Users\86151\.ssh\id_rsa" -p 26109 -N -L 18000:127.0.0.1:8000 root@connect.nmb2.seetacloud.com
```

浏览器访问 `http://127.0.0.1:18000/`。初始管理员密码只保存在服务器
`var/secrets/admin-initial-password`，不得提交 Git、复制到聊天或写入前端代码。
