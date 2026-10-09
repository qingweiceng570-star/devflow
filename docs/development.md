# M0 本地开发与启动

M0 提供登录和模型工具协议验证。三个样例是人工构造的数据，不连接 GitHub，也不输出真实 PR 的合入判断。真实模型适配已实现；当前配置为 DeepSeek（`deepseek-chat`），离线模式仍可随时回退。

## 环境与锁文件

已验证环境：Windows，Python 3.12.7，Node 24.21.0，pnpm 10.33.2，Docker Desktop Linux Engine 29.8.1。后端支持 Python 3.12–3.13；其他操作系统未进行本轮本机验收。

主要依赖：FastAPI 0.142.4、Pydantic 2.13.5、SQLAlchemy 2.1.4、Alembic 1.20.0、psycopg 3.3.6、uvicorn 0.54.0、httpx 0.28.1；Next.js 16.4.0、React 19.3.0、TypeScript 5.9.3、Playwright 1.64.0。

后端 `uv.lock` 与前端 `pnpm-lock.yaml` 是完整版本依据；不要删除锁文件或使用浮动 latest 更新。PostgreSQL 镜像固定了 digest。

## 首次准备（PowerShell，项目根目录）

```powershell
python scripts/init_env.py
python -m venv backend/.venv
backend/.venv/Scripts/python.exe -m pip install uv==0.12.23
Set-Location backend
.venv/Scripts/uv.exe sync --frozen --python .venv/Scripts/python.exe
Set-Location ..
pnpm --dir frontend install --frozen-lockfile
```

`init_env.py` 只在 .env 不存在时生成配置与随机数据库密码，不覆盖已有值。已创建虚拟环境可以直接使用，不需要再次运行 venv。pnpm 已安装时使用锁定版本；本机当前就是 10.33.2。

## 数据库与管理员

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d --wait db
pwsh -NoProfile -File scripts/dev.ps1 migrate
pwsh -NoProfile -File scripts/dev.ps1 admin
```

本项目使用独立 Compose 名称 `devflow`、独立数据卷和本机端口 55432。保留其他项目容器。

管理员用户名默认 `admin`。随机初始密码保存在 `var/initial-admin.json`，命令输出不显示密码；不要提交这个文件。重复运行初始化不重置现有账户。

需要自己设置密码时进入 backend 执行：

```powershell
.venv/Scripts/python.exe -m app.cli create-admin --username admin
```

该方式在终端隐藏输入，要求 12–128 字符；只用于还未创建的账户，不是密码重置接口。

## 本机进程方式

先构建前端，再启动三个后台进程：

```powershell
pnpm --dir frontend build
pwsh -NoProfile -File scripts/start_local.ps1
```

访问 http://localhost:3100 登录。API 文档在 http://127.0.0.1:8100/api/docs 。修改前端并希望热更新时，可以分别使用三个终端：

```powershell
pwsh -NoProfile -File scripts/dev.ps1 api
pwsh -NoProfile -File scripts/dev.ps1 worker
pwsh -NoProfile -File scripts/dev.ps1 web
```

热更新方式与 start_local.ps1 不能同时占用相同端口。本机脚本固定 Web 3100/API 8100；.env 的 WEB_PORT/API_PORT 目前用于 Compose 端口映射。

后台方式的输出在 `var/*.stdout.log` 和 `var/*.stderr.log`，进程记录在 `var/local-processes.json`。停止只关闭已记录且启动时间匹配的进程树：

启动脚本使用服务实际绑定的 `127.0.0.1` 做就绪检查，避免本机 localhost 的 IPv6 解析导致误判。浏览器仍使用 `http://localhost:3100`，与 APP_ORIGIN 保持一致。

```powershell
pwsh -NoProfile -File scripts/stop_local.ps1
```

这个命令保留数据库和数据。关闭数据库使用 `docker compose --env-file .env -f infra/compose.yaml stop db`。不要使用 `down -v`，它会删除数据库卷。

## Docker 全应用方式

完整 Compose 配置通过 `app` profile 加入迁移、API、Worker 与 Web。首次初始化管理员仍是单独的管理动作，不在每次 API 启动时自动创建。

```powershell
docker compose --env-file .env -f infra/compose.yaml --profile app build
docker compose --env-file .env -f infra/compose.yaml --profile app up -d
docker compose --env-file .env -f infra/compose.yaml --profile app run --rm api uv run --no-sync python -m app.cli create-admin --generate
```

Web 容器当前是开发服务器，便于 M0 开发；本机 `pnpm build` 另外验证了生产编译。生产部署、非 root 用户、组织权限与高可用不是本阶段的部署目标。

切换本机与 Docker 全应用方式前，先停止上一种方式的 API/Web/Worker，保留同一个数据库即可。容器通过受控 `var` 挂载共享本地记录，不把该目录公开为静态资源。

## 验证离线工具循环

登录后选择“普通 PR”“风险 PR”或“失败 CI”，点击“运行离线验证”。预期看到：

1. 一次 `read_sample` 工具调用。
2. 两次模型协议回放决策。
3. 工具调用、结果回传、结构化输出、证据绑定四项通过。
4. “离线协议回放”标记以及合成 Token 用量。
5. 刷新页面仍能查看最近记录。

离线成功不表示真实模型已通过，也不表示已完成 PR 业务审查。历史记录保存到 PostgreSQL。

## 接入真实模型

在项目根 .env 配置。当前 DeepSeek 示例（也是本仓库当前配置）：

```dotenv
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=your-server-side-key
LLM_TOKEN_PARAMETER=max_tokens
LLM_STRUCTURED_OUTPUT=json_object
```

BASE_URL 是 API 根地址，适配器追加 `/chat/completions`，不要再填写完整端点。远程地址必须 HTTPS；本机 localhost/127.0.0.1 允许 HTTP。URL 不携带密钥。

部分兼容服务只接受 `max_tokens`，可修改 `LLM_TOKEN_PARAMETER`。DeepSeek 的接口拒绝 `response_format: json_schema`（"This response_format type is unavailable now"），因此 `LLM_STRUCTURED_OUTPUT=json_object`：结构化输出改用 JSON 模式，Schema 由适配器追加到提示词，服务端 Pydantic 校验与证据绑定校验完全不变；支持严格 json_schema 的服务保持默认值 `json_schema`。决策背景见 [ADR 0002](adr/0002-structured-output-transport.md)。不支持 function tools 的服务仍返回未通过，系统不自动换成离线模式。适配格式根据 [OpenAI 官方 Chat Completions 文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) 核对。

重启 API/Worker 后点击“验证真实模型”。探测仅发送合成样例，默认最多 60 秒；会消耗配置服务的实际用量。只有成功的非 fixture 探测才记录 `live_model_verified=true`。配置错误、授权失败、响应结构错误、超时等不会回显上游敏感正文。

密钥不在浏览器录入、不回显、不导出到 OpenAPI。不要将 .env 内容贴入聊天或 Git。

## 测试与契约导出

```powershell
pwsh -NoProfile -File scripts/dev.ps1 test
backend/.venv/Scripts/ruff.exe check backend/app backend/tests backend/migrations
pnpm --dir frontend typecheck
pnpm --dir frontend build
pnpm --dir frontend test:e2e
backend/.venv/Scripts/python.exe scripts/export_contracts.py
```

后端单元/契约测试用 SQLite 隔离，不改真实本地账户；PostgreSQL 的真实迁移和 Schema 对照另行验证：

```powershell
Set-Location backend
.venv/Scripts/python.exe -m alembic check
.venv/Scripts/python.exe -m alembic current
Set-Location ..
```

E2E 需要正在运行的 Web/API、`var/initial-admin.json` 和本机 Chrome/Edge；测试会创建本地能力探测记录，不对外写入。若系统没有浏览器，先按 Playwright 官方方式安装 Chromium，或设置 PLAYWRIGHT_EXECUTABLE_PATH。当前测试适用于“模型尚未配置”的交付状态，接入模型后要相应更新按钮状态断言。

契约由实际代码生成到 `contracts/openapi.json`、`contracts/domain-schemas.json`，不要手工更改导出结果。M0 HTTP 响应当前使用统一 envelope，领域 Schema 是后续实现的基础子集。

## 常见问题

- 503 DATABASE_NOT_READY：启动数据库并执行迁移；不要在 API 启动时自动 `create_all`。
- 403 ORIGIN_REJECTED：默认浏览器入口是 localhost:3100；通过其他主机名/端口访问时需同步 APP_ORIGIN。
- 403 CSRF_FAILED：刷新或重新登录；客户端发送会话绑定的 X-CSRF-Token。
- 登录尝试限流：单 API 进程同来源一分钟最多 10 次；等待窗口结束。该实现不适用于多副本部署。
- 409 PROBE_IN_PROGRESS：等待当前诊断结束；M0 同时只跑一个探测，防止重复付费。
- Worker 等待启动：检查 `var/worker.stderr.log`；M0 Worker 只有心跳，业务队列尚未实现。
- Docker 与本地端口冲突：停止上一种方式；不停止其他项目容器。
- 模型未配置：离线验证仍可用；真实服务验证要补齐三个变量并重启后端。

## 当前边界

未实现仓库接入、同步 Job、Git 代码取证、真实 PR/CI 分析、业务 SSE、RAG、多 Agent、记忆或外部写操作。上述目录与功能按 README 的 M1–M7 顺序实现；不要将 M0 的诊断接口扩展成无界业务任务。
