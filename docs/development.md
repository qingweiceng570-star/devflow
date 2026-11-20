# M0–M7 本地开发与启动

M0 提供登录和模型工具协议验证。三个样例是人工构造的数据，不连接 GitHub，也不输出真实 PR 的合入判断。真实模型适配已实现；当前配置为 DeepSeek V4.1 Flash（API 名称 `deepseek-flash`），离线模式仍可随时回退。

M1 增加 GitHub 只读接入与增量同步、持久化 Job 队列（Worker 执行）、固定 SHA 代码工作区与证据查询，前端新增仓库页（/repos）。验收记录见 [M1 验收](demos/m1-acceptance.md)。

M2 提供 PR 审查闭环：会话与运行、ChatAgent 受控工具循环、证据引用校验、裁决规则、SSE 进度与聊天前端。验收记录见 [M2 验收](demos/m2-acceptance.md)，决策见 [ADR 0004](adr/0004-m2-run-pipeline.md)。

M3 提供知识库：文档上传与结构感知切分、BM25（+可选向量）混合检索与 RRF、索引代与 Outbox、召回测试页、`search_knowledge` 工具与知识问答模式。验收记录见 [M3 验收](demos/m3-acceptance.md)，决策见 [ADR 0005](adr/0005-m3-rag.md)。

M4–M7 增加 Issue/CI/仓库健康分析、LangGraph 发布检查、项目记忆审批、活动报告、操作草稿、只读 MCP、声明式 Skill、固定评测和恢复演练。操作方式见本文末尾，实际边界见 [平台验收记录](demos/platform-acceptance.md)。

## 环境与锁文件

已验证环境：Windows，Python 3.12.7，Node 24.21.0，pnpm 10.33.2，Docker Desktop Linux Engine 29.8.1；另已验证 Python 3.12.12 / Node 24.21.0 的 Linux 容器部署。后端支持 Python 3.12–3.13；其他宿主操作系统未进行本轮验收。

主要依赖：FastAPI 0.142.4、Pydantic 2.13.5、SQLAlchemy 2.1.4、Alembic 1.20.0、psycopg 3.3.6、uvicorn 0.54.0、httpx 0.28.1；Next.js 16.4.1、React 19.3.0、TypeScript 5.9.3、Playwright 1.64.0。

后端 `uv.lock` 与前端 `pnpm-lock.yaml` 是完整版本依据；不要删除锁文件或使用浮动 latest 更新。PostgreSQL 镜像固定了 digest。

## 首次准备（PowerShell，项目根目录）

推荐先安装 Python 3.12–3.13、Node.js 22–24、pnpm 10.33.2 和 Docker Desktop，启动 Docker Desktop 后，在项目根目录执行：

```powershell
pwsh -NoProfile -File scripts/start_local.ps1
```

该入口自动生成缺失的 `.env`、创建虚拟环境、同步锁定依赖、启动 PostgreSQL、执行迁移、确保管理员存在，并在源码变更后构建前端，最后启动 API/Worker/Web。已有配置和管理员密码保持原值。`-Check` 只检查环境和端口；`-Rebuild` 强制重建并重启本项目服务；`-SkipBuild` 复用现有构建。缺依赖或 Docker Engine 未启动时会明确报错。

失效的进程记录会自动清理；PID 被其他程序复用时不会终止该程序。未记录的端口占用会阻止启动。启动失败会清理本次启动的应用进程，保留数据库与日志。重复启动已就绪的服务会直接返回地址。

以下步骤保留为手动准备方式：

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

Windows/Hyper-V 可能把默认端口纳入保留范围，表现为端口未监听但 Docker 报 bind 权限错误。统一启动入口检测到此情况后，会从 15432–15451 中选择可绑定端口，原子更新本项目 `.env` 中的 `POSTGRES_PORT` 和本地 `DATABASE_URL`，不修改凭据、远程数据库或其他容器。显式环境变量覆盖 `.env` 时会提示人工调整。实际端口以 `.env` 为准；2026-10-26 本机已切换为 15432。

管理员用户名默认 `admin`。随机初始密码保存在 `var/initial-admin.json`，命令输出不显示密码；不要提交这个文件。重复运行初始化不重置现有账户。

首次创建管理员或需要重置已有管理员密码时进入 backend 执行：

```powershell
.venv/Scripts/python.exe -m app.cli create-admin --username admin
```

该方式在终端隐藏输入，要求 6–128 字符；只用于还未创建的账户。已有账户使用：

```powershell
.venv/Scripts/python.exe -m app.cli set-admin-password --username admin
```

重置会要求输入两次密码，并撤销该账户的已有会话。

## 本机进程方式

启动入口会检查构建是否过期，并启动三个后台进程：

```powershell
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

Web 镜像在构建阶段执行 `pnpm build`，运行时使用 `next start`。组织权限、非 root 容器和高可用仍需按部署需求单独完善。

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
LLM_MODEL=deepseek-flash
LLM_API_KEY=your-server-side-key
LLM_TOKEN_PARAMETER=max_tokens
LLM_STRUCTURED_OUTPUT=json_object
LLM_THINKING_MODE=disabled
```

BASE_URL 是 API 根地址，适配器追加 `/chat/completions`，不要再填写完整端点。远程地址必须 HTTPS；本机 localhost/127.0.0.1 允许 HTTP。URL 不携带密钥。

截至 2026-10-26，DeepSeek 官方文档指定 V4.1 Flash 使用 `deepseek-flash`，对应服务版本为 DeepSeek-V4.1-Flash-1017；实际 API 模型列表也返回该名称。版本映射见 [DeepSeek 官方说明](https://api-docs.deepseek.com/)。

V4.1 Flash 默认开启思考模式，该模式拒绝本项目能力探测使用的 `tool_choice=required`，且工具循环需要回传 `reasoning_content`。当前适配器使用普通工具调用，因此 DeepSeek 配置显式设置 `LLM_THINKING_MODE=disabled`，仍使用 V4.1 Flash 模型。其他服务保持 `provider_default` 时，请求不附加 `thinking` 参数。详见 [DeepSeek 思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode)。

部分兼容服务只接受 `max_tokens`，可修改 `LLM_TOKEN_PARAMETER`。DeepSeek 的接口拒绝 `response_format: json_schema`（"This response_format type is unavailable now"），因此 `LLM_STRUCTURED_OUTPUT=json_object`：结构化输出改用 JSON 模式，Schema 由适配器追加到提示词，服务端 Pydantic 校验与证据绑定校验完全不变；支持严格 json_schema 的服务保持默认值 `json_schema`。决策背景见 [ADR 0002](adr/0002-structured-output-transport.md)。不支持 function tools 的服务仍返回未通过，系统不自动换成离线模式。适配格式根据 [OpenAI 官方 Chat Completions 文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) 核对。

重启 API/Worker 后点击“验证真实模型”。探测仅发送合成样例，默认最多 60 秒；会消耗配置服务的实际用量。只有成功的非 fixture 探测才记录 `live_model_verified=true`。配置错误、授权失败、响应结构错误、超时等不会回显上游敏感正文。

密钥不在浏览器录入、不回显、不导出到 OpenAPI。不要将 .env 内容贴入聊天或 Git。

## 测试与契约导出

```powershell
pwsh -NoProfile -File scripts/dev.ps1 test
backend/.venv/Scripts/ruff.exe check backend/app backend/tests backend/migrations
pnpm --dir frontend typecheck
pnpm --dir frontend test:unit
pnpm --dir frontend build
pnpm --dir frontend test:e2e
backend/.venv/Scripts/python.exe scripts/export_contracts.py
```

后端单元/契约测试用 SQLite 隔离，不改真实本地账户。两个队列并发用例（`-m pg`）需要可连接的 PostgreSQL：默认从项目 .env 派生 `devflow_test` 库（自动创建），数据库不可达时自动跳过。PostgreSQL 的真实迁移和 Schema 对照另行验证：

```powershell
Set-Location backend
.venv/Scripts/python.exe -m alembic check
.venv/Scripts/python.exe -m alembic current
Set-Location ..
```

E2E 需要正在运行的 Web/API、`var/initial-admin.json` 和本机 Chrome/Edge；测试会创建本地能力探测记录，不对外写入。若系统没有浏览器，先按 Playwright 官方方式安装 Chromium，或设置 PLAYWRIGHT_EXECUTABLE_PATH。E2E 假定模型已配置（“验证真实模型”按钮可用）；若临时改回未配置状态，需要同步调整该断言。

契约由实际代码生成到 `contracts/openapi.json`、`contracts/domain-schemas.json`，不要手工更改导出结果。M0 HTTP 响应当前使用统一 envelope，领域 Schema 是后续实现的基础子集。

## M1：仓库同步、任务队列与代码证据

Worker 现在会消费队列任务：启动本地栈（或 Docker 的 worker）后，同步请求会由 Worker 执行。全部接口只读 GitHub，写操作要到 M6 才存在。

### 仓库与同步

在浏览器使用 `http://localhost:3100/repos`（支持 `owner/repo`、GitHub 仓库主页链接、仓库子页面链接、`.git` 克隆地址、`git clone` 命令和 SSH 克隆地址；页面会先显示解析出的仓库名），或使用 API：

```bash
curl -b cookies.txt -H "Origin: http://localhost:3100" -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" -d '{"full_name":"owner/repo"}' http://127.0.0.1:8100/api/v1/repos
```

```bash
curl -b cookies.txt -H "Origin: http://localhost:3100" -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" -d '{}' http://127.0.0.1:8100/api/v1/repos/<repo_id>/sync
```

`GET /api/v1/sync-runs/{id}` 查看状态与逐资源计数；`GET /api/v1/jobs/{id}` 查看任务（不暴露租约与 payload）。同一仓库已有活动同步时，重复请求返回现有运行（`created:false`）；可选 `Idempotency-Key` 头按 24 小时重放同键同 body 的响应。

### 同步边界与网络出口

默认每资源每轮最多 4 页、closed 窗口 90 天、CI 窗口 30 天，超限记为 partial 且不推进水位（下次续扫）。配额 403/429 映射为可重试错误，由队列按 retry_after 自动重排（上限 15 分钟一档）。边界细节见 [ADR 0003](adr/0003-m1-sync-queue-workspace.md)。

三个出口配置（默认适合直连网络）：

- `GITHUB_TRUST_ENV=false`：api.github.com 请求忽略系统代理直连。若你的网络要求代理访问 API，改为 `true`（Windows 会读取系统代理设置）或配置显式代理。
- `GITHUB_API_PROXY`：只用于 GitHub API 的显式 HTTP(S) 代理（例如 `http://127.0.0.1:7897`），优先于 `GITHUB_TRUST_ENV`。导入公开仓库时若提示“无法连接 GitHub”，先确认本地代理已启动，再设置这一项或把 `GITHUB_TRUST_ENV` 改为 `true`，然后重启 API/Worker。
- `GITHUB_GIT_PROXY`：git fetch/clone 使用的 HTTP(S) 代理（例如 `http://127.0.0.1:7897`）。直连 github.com 的 git 端点被重置时填写；凭据与代理通过环境变量注入 git，不进入命令行或配置文件。

导入仓库时会自动识别仓库主页、子页面、`.git` 克隆地址、`git clone` 命令、`git@github.com:owner/repo` 和 `ssh://git@github.com/owner/repo` 等形式；重命名或转移过的仓库会跟随 GitHub 的同源重定向。

公开仓库的匿名 API 额度按出口 IP 计算（每小时 60 次），共享代理出口很容易耗尽。额度用尽时，导入会自动改用 `git ls-remote`（走 `GITHUB_GIT_PROXY`）校验仓库并读取默认分支，因此公开仓库仍可接入；后台同步任务会在额度恢复后重试。配置 `GITHUB_READ_TOKEN` 后额度提升到每小时 5000 次，可避免这一限制。

私有仓库需在 .env 填写 `GITHUB_READ_TOKEN`（只读、最小权限）后重启后端；Token 不经浏览器、不回显、不写入日志。

### 固定 SHA 读取与证据

```bash
backend/.venv/Scripts/python.exe scripts/m1_demo_evidence.py simonw/ttok README.md
```

脚本从已同步 PR（或默认分支 HEAD）取 SHA，克隆/更新本地镜像，按固定版本读取文件并记录证据；返回的 `evidence_id` 可通过 `GET /api/v1/evidence/{id}` 核查（片段、locator、内容哈希、源链接）。工作区位于 `var/workspaces/<repo_id>`，全部为 bare mirror，读取不 checkout 可变分支。

### 队列与恢复的运行检查

Worker 每个轮询周期心跳一次；租约 60 秒、每页边界续期；Worker 被强杀后，过期租约在恢复周期（默认 60 秒）内被回收：可重试任务回到 retry_wait，次数用尽则任务与同步运行一并标记失败。集成测试在真实 PostgreSQL 上验证这些行为（见下）。

## M2：PR 审查会话与运行

### 使用方式

浏览器：仓库列表 → 「进入」→ 「新建分析会话」→ 填 PR 编号与关注点 → 「提交分析」。运行期间页面由 SSE 实时展示工具轨迹，可点击「取消当前分析」；完成后按 结论 → 风险发现（带证据芯片）→ 待验证推测 → 证据缺口 → 建议下一步 → 局限性 展示。刷新页面自动恢复。

API 主线（均需登录 Cookie；写操作需 `Origin` 与 `X-CSRF-Token`）：

- `POST /api/v1/conversations`（body 含 repo_id）→ 会话
- `POST /api/v1/conversations/{id}/runs`（`{"message","kind":"pr_review","target":{"pr_number":N}}`）→ 202，可带 `Idempotency-Key`；会话内已有活动运行返回 409 `ACTIVE_RUN_EXISTS`
- `GET /api/v1/runs/{id}` / `GET /api/v1/runs/{id}/tools` / `POST /api/v1/runs/{id}/cancel`
- `GET /api/v1/runs/{id}/events`：SSE；支持 `Last-Event-ID` 或 `?after=` 断线恢复，15 秒心跳注释，终态后关闭

### 边界与行为

- 分析任务 `max_attempts=1`：失败（含被中断）即终态，不做自动重放；重新分析请新建运行（见 ADR 0004）。
- 裁决只降级不升级；正结论要求：无阻塞级发现、coverage 完整、目标版本未变化、mergeable 为真且**必需检查已配置并全部通过**。必需检查通过管理员配置：`PATCH /repos/{id}` 携带 `required_checks`（工作流名称列表）与 `expected_version`。
- 工具结果双重封顶：单次 64 KiB（TOOL_RESULT_MAX_BYTES）与每运行累计 256 KiB（TOOL_BYTES_TOTAL_LIMIT）；超出返回 `TOOL_BUDGET_EXCEEDED`/`TOOL_RESULT_TOO_LARGE`，模型据此收束。
- 输出上限 MODEL_OUTPUT_TOKEN_LIMIT 默认 3000；终稿因长度被截断时循环会要求一次精简重写，两次仍截断才失败。

### SSE 与代理说明

Next.js 代理对 `Accept: text/event-stream` 的请求透传流式响应（不缓冲、不加 65 秒超时）；普通请求仍为有界缓冲。若绕过 Next 直连 8100，前端需自行处理 CORS 与 Cookie。

## M3：知识库与检索

### 直接使用 GitHub 仓库文档

新接入仓库时，后台会自动导入一次默认分支文档。已有仓库进入「知识库」点击「从 GitHub 导入 / 更新文档」即可，无需逐份上传。导入完成会自动建立索引，页面显示导入、索引和就绪状态；问答页选择「知识问答」也会检查资料是否已准备好。

自动提取 README、根目录指南以及 `docs/`、`doc/`、`documentation/` 下的 UTF-8 Markdown、MDX、RST 和纯文本；不执行 MDX、脚本或文档中的指令。每次最多 200 份、总计 10 MiB、单份 1 MiB，跳过项会显示原因。其他资料可手动上传，每份最多 5 MiB。

仓库文档以 `git:路径` 保存，上传资料以 `upload:文件名` 保存，同名也不会相互覆盖。再次导入会复用未变化内容、更新变化文档，并停用仓库中已删除的文档。每份资料保留来源提交和历史版本，页面可打开对应 GitHub 原文。导入任务中断后可重试，旧任务不能发布失效结果。

知识库按仓库隔离，不是模型自带的通用资料库。「召回测试」用于检索文档段落；需要模型解释和归纳时，进入项目会话选择「知识问答」。尚未配置向量服务时仍可使用关键词检索，查询尽量包含项目名、功能名或文档中的词。

相关接口：`POST /repos/{id}/knowledge/import` 提交异步任务，`GET /repos/{id}/knowledge/status` 返回文档数量、来源统计、导入结果和索引任务状态。
### 使用方式

浏览器：仓库总览 → 「知识库」→ 上传文档（.md/.markdown/.txt，≤5 MiB）→ 自动触发索引 → 「召回测试」框检索并查看通道 trace。会话页的「类型」选择「知识问答」即可进行带引用的项目问答。

API 主线：

- `POST /api/v1/repos/{id}/documents`（multipart，可选 `source_revision`）→ 202；同内容重传 `changed=false`
- `GET /api/v1/repos/{id}/documents` / `GET /api/v1/documents/{id}`（版本与块数）
- `DELETE /api/v1/documents/{id}`（body `expected_version`；tombstone 后检索立即排除，重建清理异步进行）
- `POST /api/v1/repos/{id}/knowledge/search`（只检索不建运行，返回结果 + 完整 trace）
- `GET /api/v1/repos/{id}/knowledge/status` / `POST /api/v1/repos/{id}/knowledge/reindex`

### 索引与降级

- 每次内容变更写 Outbox 行并入队 index Job（Worker 执行，全量重建索引代，激活时旧代转 superseded，最多保留两代）。
- `INDEX_DEBOUNCE_SECONDS` 默认 2 秒，将窗口内的连续上传合并进一个索引任务。每次新索引只包含文档当前版本；旧版本保留供历史证据核查。
- BM25 使用标准对数 IDF；进程内最多缓存 8 代索引，按估算对象大小限制在 64 MiB，访问时检查文件状态。向量按仓库、服务地址、模型、维度和内容哈希复用，成功激活后只保留当前内容的缓存；缓存损坏会重新生成。
- 所有检索通道失效时返回 `503 RETRIEVAL_UNAVAILABLE`，不会将故障解释为没有资料。知识库页面可使用“重建索引”恢复。
- 只接受 UTF-8 Markdown、MDX、RST 和纯文本；PDF 等格式明确拒绝（415）。目标 600/上限 900/重叠 80 token 的结构感知切分；分词器 `zh-en.v1`（英文标识符保留+拆词、中文 bigram）与切分器版本随索引代记录。
- 向量通道可选：`uv sync --group knowledge` 安装 pymilvus，`docker compose --profile knowledge up -d` 启动 Milvus，配置 `EMBEDDING_BASE_URL/MODEL`（OpenAI 兼容 /embeddings）与 `MILVUS_URI` 后重建索引。未配置时 BM25 单路运行并在页面/接口显式标记 `not_configured`；通道故障时标 `*_unavailable` 降级，双路故障返回 `RETRIEVAL_UNAVAILABLE`。
- Rerank 可选：配置 `RERANK_BASE_URL`（Jina/Cohere 兼容的 `POST /rerank`）、`RERANK_MODEL`，需要鉴权时再填 `RERANK_API_KEY`。RRF 融合后对前 `RAG_RERANK_K` 个候选重排并取 `RAG_FINAL_K`；未配置时保留 RRF 顺序并标记 `not_configured`，调用失败标记 `rerank_unavailable` 降级回 RRF，不会让检索整体失败。
- 删除/过滤在 DB 回查层即时生效，不依赖重建；历史文档引用固定到 document_version，不会被最新正文静默替换。

## 常见问题

- 503 DATABASE_NOT_READY：启动数据库并执行迁移；不要在 API 启动时自动 `create_all`。
- 403 ORIGIN_REJECTED：默认浏览器入口是 localhost:3100；通过其他主机名/端口访问时需同步 APP_ORIGIN。
- 403 CSRF_FAILED：刷新或重新登录；客户端发送会话绑定的 X-CSRF-Token。
- 登录尝试限流：单 API 进程同来源一分钟最多 10 次；等待窗口结束。该实现不适用于多副本部署。
- 409 PROBE_IN_PROGRESS：等待当前诊断结束；M0 同时只跑一个探测，防止重复付费。
- Worker 等待启动：检查 `var/worker.stderr.log`；Worker 支持同步、分析、索引和批准后的操作队列；检查最后心跳与 Job 状态。
- Docker 与本地端口冲突：停止上一种方式；不停止其他项目容器。
- 模型未配置：离线验证仍可用；真实服务验证要补齐三个变量并重启后端。

## 当前实现与边界

M0–M7 学习版代码已接通。当前真实模型是本地配置的 OpenAI 兼容接口，离线验证始终明确标识为样例。功能、验收证据和外部依赖限制见 [完整验收记录](demos/platform-acceptance.md)。

## 日常操作

1. 在工作区根目录运行 `./scripts/start_local.ps1`，打开 http://localhost:3100 。关闭服务用 `./scripts/stop_local.ps1`；这两个脚本只管理本项目记录的进程。
2. 进入仓库 → 新建分析会话，选择 PR、Issue、CI、仓库健康或知识问答。CI 要填写准确的 run ID 和 attempt，job ID 可选。
3. 进入「工作流与审批」→ 发布检查，填写 PR；CI 可指定，也可在当前返回范围中匹配同 SHA。自定义 DAG 最多 8 个节点，并行最多 3 个。
4. 「项目记忆」提交已完成运行的经验和证据，批准后才参与后续上下文。过期/撤销经验不参与新召回；同键冲突需明确选择替代项。
5. 「操作草稿」完整审阅目标、正文、版本和有效期，再批准；执行是另一步。修订会清除批准。unknown 只能核查，不会自动重发。
6. 「协作与报告」使用 UTC 输入统计范围；报告只统计已同步事件。可下载 JSON，批准后作为 generated 文档加入知识库。
7. 「扩展」安装版本化声明式 Skill 或经过审查的只读 MCP。安装后在新会话中要求使用对应 Skill；旧 Run 保留启动时固定版本，停用会阻止后续调用。

### 可选配置

```dotenv
WORKFLOW_TIMEOUT_SECONDS=600
WORKFLOW_TOKEN_BUDGET=60000
WORKFLOW_PARALLELISM=3
ENABLED_ACTIONS=[]
WRITE_REPO_ALLOWLIST=[]
MCP_SERVER_ALLOWLIST=[]
```

JSON 数组写在本地 `.env`，修改后重启 API/Worker。例如只在专用测试仓库启用评论：`ENABLED_ACTIONS=["create_issue_comment"]`、`WRITE_REPO_ALLOWLIST=["your-account/test-repo"]`，并单独配置 `GITHUB_WRITE_TOKEN`。页面批准不能绕过这些服务端开关。首次真实写回需要自行确认测试仓库与完整草稿内容。

MCP 固定地址需在 `MCP_SERVER_ALLOWLIST` 内，远端必须支持 Streamable HTTP。首版不启动 stdio 命令，敏感凭据可由管理员控制的鉴权网关处理；禁止把密钥放进页面 URL/配置正文。

### 重复验收

```powershell
cd backend
.venv/Scripts/ruff.exe check app tests
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m alembic check
cd ..
backend/.venv/Scripts/python.exe scripts/export_contracts.py
backend/.venv/Scripts/python.exe scripts/evaluate.py
cd frontend
pnpm build
pnpm exec playwright test
```

真实模型验收脚本 `scripts/m4_smoke.py`、`scripts/release_smoke.py` 会发送仓库内容并产生模型费用，只在你明确选择该仓库和提供商后手动运行。默认回归测试不会发送真实仓库给模型。

### 备份与恢复

先停止本项目所有写入进程；如果使用 Docker app profile，也需停 api/worker。备份不包含 `.env` 或初始管理员明文凭据，应另行安全保存配置。

```powershell
./scripts/stop_local.ps1
backend/.venv/Scripts/python.exe scripts/backup_restore.py backup var/backups/my-backup
backend/.venv/Scripts/python.exe scripts/backup_restore.py verify var/backups/my-backup
backend/.venv/Scripts/python.exe scripts/backup_restore.py restore var/backups/my-backup --database devflow_restore_check --storage var/restore-check
backend/.venv/Scripts/python.exe scripts/verify_restore.py --database devflow_restore_check --storage var/restore-check
./scripts/start_local.ps1
```

恢复命令只接受 `devflow_restore_` 开头的新数据库，目标对象目录必须不存在，绝不覆盖原库。验证后若要实际切换，更新本地 `DATABASE_URL` 与 `STORAGE_ROOT` 并重启；在知识库页面重新索引。Git 工作区缓存会按固定 SHA 重新获取。恢复库含账号哈希、历史会话和项目证据，应按项目数据保管。

### 独立 Docker 安装验收

```powershell
docker compose -p devflow-acceptance --env-file .env -f infra/compose.yaml -f infra/compose.acceptance.yaml --profile app up -d --build
docker compose -p devflow-acceptance --env-file .env -f infra/compose.yaml -f infra/compose.acceptance.yaml --profile app exec -T api uv run --no-sync python -m app.cli create-admin --generate
backend/.venv/Scripts/python.exe scripts/docker_acceptance.py
```

独立项目使用数据库 55433、API 18100、Web 13100 和 `var/clean-acceptance`；不会复用开发数据库卷。首次启动仍需按前文步骤创建管理员。网络需要代理时，可仅给 `docker compose build` 传构建代理参数；不需要切换 Docker 的镜像存储后端。完成后用相同参数执行 `stop` 可停止这组验收容器，保留数据。

### 真实基线与独立流程验收

```powershell
backend/.venv/Scripts/python.exe -X utf8 scripts/evaluate_real.py
cd frontend
node node_modules/@playwright/test/cli.js test --config playwright.flows.config.ts
cd ..
```

真实基线评分使用已冻结的公开案例，不请求模型；指标定义和人工复核状态见 [评测说明](../evals/README.md)。`export_real_cases.py` 是首次冻结脚本，已有 real-v1 时拒绝覆盖；新增案例应建立新版本。

独立浏览器验收先要求已完成 `pnpm --dir frontend build`，自动启动 18110/13110 测试进程，每次使用 `var/e2e/` 下的新数据库。GitHub 使用模拟读接口，不配置写令牌或模型密钥。验收结束自动关闭测试服务；控制端点不在正式应用内。

日常使用时：在会话选择 PR/Issue/CI；发布检查先选 PR，再选择同提交 CI 或让运行时自动匹配；项目经验先选来源分析再勾选支持证据。候选列表每类最多 100 条已同步记录，未列出的分析目标可手动输入。

在「工作流与审批」点击「检查 GitHub 读取权限」，分别查看仓库与 Actions 能力。Actions 列表可读不证明日志可读。需要日志时，在本地 `.env` 配置覆盖目标仓库且具有 Actions: read 的 GitHub 读取令牌并重启；不要把令牌放在页面或聊天中。对于 404，先核对目标与权限，不能单凭 404 判断过期。
