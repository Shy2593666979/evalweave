# EvalWeave

让 AI 的每一步，都可以被度量。

EvalWeave 是一个面向 AI 应用的开源评测与执行追踪平台。它计划支持数据集管理、工具调用评测、中间链路检查、LLM-as-a-Judge、人工评分，以及实验版本对比。

## 技术栈

- Python 3.12、uv、FastAPI、SQLModel
- Celery、Redis、MySQL 8
- Vue 3、TypeScript、Vite、Element Plus
- 所有应用配置均从 YAML 文件读取，不读取环境变量

## 当前账号能力

- 用户名和密码注册、登录、退出
- HttpOnly Cookie 登录会话和接口权限校验
- Admin 直接创建、启用或停用用户
- Admin 自定义用户类型及其权限
- 注册时选择 Admin 设置为“注册可选”的用户类型
- 默认用户类型：产品同学、用研同学、研发同学、测试同学
- 项目文件上传、列表、下载和删除，本地文件内容与数据库元数据分离存储
- 评测 Agent 自动探查 JSON、JSONL、CSV、Excel，生成受限 EvalSpec 并通过 Celery 执行
- 人工审批任务及企业微信群机器人、SMTP 邮件通知

本地配置首次启动会创建一个 Admin：

- 用户名：`admin`
- 密码：`evalweave-admin`

正式部署前必须修改 `config/application.yaml` 中的初始密码和 JWT 密钥。

## 本地开发

安装依赖、启动 MySQL/Redis，并启动 API、Celery 和前端：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1
```

需要先启动 Docker Desktop。停止时按 `Ctrl+C`。

也可以逐项启动：

1. 创建本地配置：

   ```powershell
   Copy-Item config/application.example.yaml config/application.yaml
   ```

2. 安装 Python 依赖：

   ```powershell
   uv sync
   ```

3. 启动 MySQL 和 Redis：

   ```powershell
   docker compose -f deploy/docker-compose.yaml up -d
   ```

4. 同时启动 API 和 Celery Worker（API 启动时会自动创建尚不存在的数据表）：

   ```powershell
   uv run evalweave --config config/application.yaml
   ```

   按 `Ctrl+C` 会同时停止两个进程。OpenAPI 文档位于 <http://127.0.0.1:8000/docs>。

   如需分别启动，也可以使用两个终端运行：

   ```powershell
   uv run evalweave-api --config config/application.yaml
   uv run evalweave-worker --config config/application.yaml
   ```

5. 启动前端：

   ```powershell
   Set-Location web
   pnpm install
   pnpm dev
   ```

   控制台位于 <http://127.0.0.1:5173>。

## 质量检查

```powershell
uv run ruff check .
uv run pytest
Set-Location web
pnpm typecheck
pnpm build
```

## 评测 Agent 与通知

Agent 默认关闭。请在 `config/application.yaml` 中配置 OpenAI Python SDK 使用的模型接口；
`agent.api_mode` 默认使用 Responses API，兼容服务仅实现 Chat Completions 时可改为
`chat_completions`；
目标接口无需配置主机白名单；请求头只允许放在服务端 YAML 的 `agent.target_headers` 中，
不接受任务提交者传入密钥。

企业微信首版使用群机器人 Webhook，邮件使用 SMTP。通知内容包含平台人工任务链接，
批准或拒绝操作统一在平台内完成并留存审计记录。完整配置字段见
`config/application.example.yaml`。

登录后可从“评测任务”完成项目选择、数据上传、任务启动、方案审核、运行跟踪和结果下载。
运行中的任务会自动刷新状态；API 服务之外还需要同时启动 Redis 和 Celery Worker。
评测助手支持通过对话整理接口地址、返回示例和评测要求，任务结果可导出为 Excel、
JSONL、Markdown 或纯文本。Admin 可在“评测模型”中维护可选模型，API Key 加密保存且不回显。

## 许可证

Apache License 2.0
