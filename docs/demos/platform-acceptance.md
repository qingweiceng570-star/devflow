# M4–M7 实现与验收（2026-10-15）

本记录区分确定性功能验收、真实模型运行与尚未配置的外部能力。离线用例通过不代表模型回答准确率。

## 交付范围

| 模块 | 实现位置 | 使用入口与边界 |
| --- | --- | --- |
| Issue 分诊 | `backend/app/agents/specialists.py`、`jobs/handlers/specialists.py` | 会话页；目标是已存在的 Issue。优先级、负责人、重复候选均需可核查依据；不自动发评论。 |
| CI 排障 | `ingestion/ci_logs.py`、`jobs/handlers/specialists.py` | 会话页；固定 run/attempt/job，纯文本日志按行取窗口、脱敏、有界下载；无日志时保留未知。 |
| 仓库健康 | `tools/github_tools.py`、`jobs/handlers/analyze.py` | 会话页；统计来自已同步范围，不推断远端全量或 CI 全部通过。 |
| 发布检查 | `workflows/contracts.py`、`workflows/release.py` | 工作流与审批页；LangGraph 的 Planner → Scheduler → Observer → Synthesis，默认 PR + 同提交 CI；可提交类型化 DAG。 |
| 共享预算与恢复 | `agents/budget.py`、`jobs/recovery.py` | 原子预留、逐调用结算；子 Agent 最多使用 85% 总额度。完成节点复用，中断调用保守计费。 |
| 项目记忆 | `services/memory_service.py` | 记忆候选 → 审批 → 不可变内容版本；同键冲突需明确替代；支持过期、路径/提交范围与撤销。 |
| 上下文 | `context/assembler.py`、`jobs/handlers/execution.py` | 冻结会话序号，近期消息 + 有来源的历史摘录 + 已批准经验；原始消息保留。历史摘录不充当新的事实证据。 |
| 活动报告 | `workflows/weekly.py` | 按 `[start,end)` 事件发生时间计数；下载 JSON，人工批准后作为 generated 文档入库。无自动反馈入库循环。 |
| 审批写回 | `services/action_service.py`、`tools/draft_tools.py` | Agent 只能准备本地草稿。页面完整展示正文/目标/版本；批准、执行分离。允许评论、已有标签、新建 Issue、关闭 Issue。 |
| 审计 | `persistence/models/platform.py`、平台迁移 | 追加审计事件；PostgreSQL 触发器拒绝 UPDATE/DELETE。数据库管理员仍可禁用触发器，不声称密码学防篡改。 |
| MCP | `extensions/mcp.py`、`extensions/registry.py` | 固定地址白名单、已审查只读工具、仓库参数服务端注入、Schema 变化暂停、30 秒/64 KiB 上限；支持 Streamable HTTP JSON/SSE。 |
| Skill | `extensions/skills.py` | 声明式工具、条件、转换、单层 Agent 委派；禁止脚本、eval、递归和动态安装。工具共享执行预算与取消检查。 |
| 评测 | `evals/dataset-v1.json`、`scripts/evaluate.py` | 40 个固定合成用例，分开发/回归集；记录数据集哈希和版本。仅确定性边界与 BM25 召回评测。 |
| 运维 | `scripts/backup_restore.py`、`infra/compose.acceptance.yaml` | 停写备份、逐文件 SHA-256 校验、只恢复到新库和新目录；独立 Compose 项目验收。 |

## 确定性验收

- 完整后端测试：249 项通过。覆盖 SQLite 状态机及真实 PostgreSQL 预算竞争、并行 DAG、一次冲突补查与节点复用，以及 MCP 仓库参数注入、Schema 变化停用、非法响应处理。
- 前端 TypeScript 检查和生产构建通过。
- Playwright：7 项通过，覆盖登录、离线模型协议回放、仓库入口、全部协作页面、真实本地周报生成/下载和 390 px 移动布局。
- 固定评测：40/40 通过；PR 10、CI 8、Issue 6、知识 8、工作流 8。结果见 [`../../evals/results/offline-latest.json`](../../evals/results/offline-latest.json)。这不是 40 个真实模型回答的质量评测。
- 数据库从 `0004_m3` 升级至 `02d6b649101f`，`alembic check` 未发现 ORM/迁移差异。
- 原有 Docker 容器、镜像和数据库未删除；独立验收使用 `devflow-acceptance` 项目名，不切换 Docker 镜像存储设置。

## 独立 Docker 验收

镜像从锁文件构建，使用独立空数据库和对象目录，迁移链完整升级至 `02d6b649101f`。已验证 Web 13100 → API 18100 的代理、登录 Cookie/CSRF、Worker 心跳、离线模型协议回放、后台周报完成及 SSE 事件回放。脚本：`scripts/docker_acceptance.py`；结果：`var/docker-acceptance-result.json`。

构建遇到下载中断后，为后端构建指定本机代理，前端构建指定 `NPM_REGISTRY=https://registry.npmmirror.com`，所有锁文件完整性校验保留。默认 Dockerfile 仍使用官方源，镜像源选择仅影响本次构建。验收未访问真实模型或 GitHub。

验收后已停止 `devflow-acceptance` 容器并保留其数据与镜像。日常使用的本机 API/Worker/Web 仍运行，入口为 `http://localhost:3100`。

## 已获授权的真实模型验收

目的地：`api.deepseek.com` / `deepseek-chat`。输入仅限公开仓库 `simonw/ttok` 的必要片段；本轮没有执行 GitHub 外部写入。

| 目标 | 运行 ID | 结果 |
| --- | --- | --- |
| Issue #25 | `5bfcc3bf-b36a-484e-a484-04b4f86fc79f` | completed；fresh / partial；2 条带证据发现。缺少 CODEOWNERS 时不编造负责人。 |
| CI 37859163438 / attempt 1 | `3a9b49af-45d8-4d98-a828-aca34820de77` | completed；fresh / partial；日志请求返回 403，明确说明无法判断根因。 |
| PR #16 发布检查 | `4159745a-72c6-48d4-a0b6-beb21397f397` | PR 子任务 completed；当前 CI 查询范围没有同 SHA 结果，最终 insufficient_evidence。 |

原始本地记录：`var/m4-live-results.json`、`var/release-live-result.json`。本轮执行了 Issue、CI、PR 三个真实业务 Agent 分析；发布汇总为确定性规则，不额外调用模型。真实并行 CI 成功链路未在此老 PR 上形成，已由 PostgreSQL 集成测试覆盖其执行机制。

## 备份恢复演练

- 备份：`var/backups/platform-20261015`，103 个文件全部通过哈希校验。
- 恢复到新库：`devflow_restore_20261015`。
- 对象目录：`var/restore-20261015`。
- 恢复后读取：迁移 `02d6b649101f`、1 个仓库、11 个运行、63 个证据记录。
- 使用 `scripts/verify_restore.py` 校验恢复库的 41 个来源快照，全部对象哈希一致；重建 35 块 BM25 索引，检索返回 6 条结果，未调用模型。
- 原开发库未覆盖。备份排除 `.env`、初始管理员明文凭据、Git 工作区缓存及派生索引；切换至恢复库后需重新配置凭据并重建知识索引。

## 配置与交付边界

1. `ENABLED_ACTIONS=[]` 与 `WRITE_REPO_ALLOWLIST=[]` 默认关闭写回。四类操作的版本、去重、超时和核查机制通过模拟服务验证；真实 GitHub 写入没有获得本轮授权，因此未发送测试评论。
2. 向量检索与第三方 MCP 是可选配置。本轮未新增 embedding 服务、Milvus 实例或真实第三方 MCP 账号；混合检索降级、MCP 协议与边界由本地测试覆盖。
3. Planner 使用服务端受控默认计划或用户提供的经过校验的 DAG，不让模型自行扩大仓库/权限范围。Observer 最多补查一次；缺口无法解决时保留缺口。
4. 工作流持久化采用业务表 `task_runs` / `workflow_checkpoints`；重启会重新进入编排并复用已提交节点，不是逐 token 回放。外部写入不进入恢复图。
5. MCP 首版使用 Streamable HTTP，不支持启动任意 stdio 进程；需要专用凭据的 MCP 可放在管理员控制的鉴权网关后。扩展工具自称只读不能代替管理员审查。
6. CI 只处理 GitHub 返回的纯文本 Job 日志，不解压未知压缩包；日志权限不足或过期时不会假造日志引用。
7. 首版检索提供 BM25/向量 RRF 融合；`RERANK_MODEL` 为预留配置，目前不会调用重排模型。运行记录中 `reranked=false`，不把融合结果描述成已重排。

设计依据：[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)。实际接口以 `contracts/openapi.json` 为准。
