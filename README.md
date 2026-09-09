# EvalWeave

让 AI 的每一步，都可以被度量。

EvalWeave 是一个面向 AI 应用的开源评测与执行追踪平台。它计划支持数据集管理、工具调用评测、中间链路检查、LLM-as-a-Judge、人工评分，以及实验版本对比。

## 技术栈

- Python 3.12、uv、FastAPI、SQLModel、Alembic
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

本地配置首次启动会创建一个 Admin：

- 用户名：`admin`
- 密码：`evalweave-admin`

正式部署前必须修改 `config/application.yaml` 中的初始密码和 JWT 密钥。

## 本地开发

安装依赖、启动 MySQL/Redis、执行迁移并启动 API、Celery 和前端：

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

4. 生成并执行首个数据库迁移：

   ```powershell
   uv run alembic upgrade head
   ```

5. 启动 API：

   ```powershell
   uv run evalweave-api --config config/application.yaml
   ```

   OpenAPI 文档位于 <http://127.0.0.1:8000/docs>。

6. 启动 Celery Worker：

   ```powershell
   uv run evalweave-worker --config config/application.yaml
   ```

7. 启动前端：

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

创建新迁移时使用：

```powershell
uv run alembic revision --autogenerate -m "describe the change"
```

## 许可证

Apache License 2.0
