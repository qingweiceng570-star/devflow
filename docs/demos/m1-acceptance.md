# M1 验收记录

日期：2026-10-09。范围：GitHub 只读接入与增量同步、持久化 Job 队列、固定 SHA 代码工作区、证据采集与查询、仓库前端页。

测试仓库：`simonw/ttok`（公开仓库，GitHub external_id 642509669，默认分支 main，验收时 open 10 / 体积 39 KB）。凭据：匿名只读（未配置 GITHUB_READ_TOKEN）。

## 实测结果

| 检查 | 结果 | 证据与限制 |
| --- | --- | --- |
| 仓库接入 | passed | `POST /repos` 真实调用 GitHub 读取元数据；同一仓库重复接入按 external_id 去重返回 `created:false` |
| 首次同步（全部资源） | passed | 16 秒完成：Issue 13、PR 7、Review 0（3 个开放 PR 各读取一次）、CI 31（runs+jobs）、事件 0（28 条未映射类型跳过）；`coverage=complete`，无错误 |
| 重复同步幂等 | passed | 立即二次同步 `created` 全为 0；未变化的列表 ETag 命中 304（`fetched=0`）；closed 扫描被 watermark 截止；CI 按新时间窗重拉但仅更新（`updated=10`） |
| 限流处理 | passed（真实命中） | 首次注册时真实触发匿名配额 403：返回 `SOURCE_RATE_LIMITED`（retryable，带 retry_after）；诊断为出口 IP 问题（见下）。限流等待/重试/回放逻辑另由离线测试逐项覆盖（20+ 场景） |
| 固定 SHA 读取 | passed | 使用已同步 PR #22 的 head `ad5899883f13b581ad5dc51f648be480857e6ab6`；bare mirror 克隆（经本机代理）后读取 README.md 259 行，`truncated=false`；仓库外/二进制/超限/越界路径均由策略拒绝 |
| 证据可核查 | passed | `scripts/m1_demo_evidence.py` 记录证据 `6007083a-621f-417a-84c1-0a7cfd9b8a3b`；`GET /evidence/{id}` 返回 excerpt、locator（commit/path/行号/对象引用）、content_hash、GitHub 源链接；对象文件以内容哈希落盘 |
| 队列与恢复 | passed | 真实 PostgreSQL 集成测试：两个 worker 并行领取互不阻塞（SKIP LOCKED）、旧 epoch 写入被拒绝、过期租约回收为 retry_wait、重试耗尽后运行标记 failed（WORKER_INTERRUPTED） |
| 后端测试 | passed | 114 passed（新增 68）：同步引擎（MockTransport 回放 6 场景）、队列 10、HTTP 客户端 12、工作区 8、路径策略 24、仓库 API 9、证据服务 5；含 2 个真实 PostgreSQL 并发用例 |
| 代码检查 | passed | Ruff（app/tests/migrations） |
| 前端 | passed | `pnpm typecheck`、`pnpm build`（新增 /repos 页面）；Playwright 3 passed（登录/移动端/仓库页导航）；1440px 截图 [var/m1-repos.png](../../var/m1-repos.png) |
| 契约导出 | passed | `contracts/openapi.json` 已包含仓库/同步/任务/证据接口 |

## 过程中的真实问题与处置

1. **出口 IP 与限流**：本机 httpx 默认使用 Windows 系统代理（127.0.0.1:7897，代理节点出口 50.7.158.238），该共享出口的 GitHub 匿名配额已耗尽，注册请求真实返回 403 rate limit。处置：新增 `GITHUB_TRUST_ENV`（默认 false，API 直连）与 `GITHUB_GIT_PROXY`（git 走本机代理，直连 github.com 的 git 端点被重置）。决定记录于 [ADR 0003](../adr/0003-m1-sync-queue-workspace.md)。
2. **sqlite 时区/夹具问题**：本地测试夹具对 `localhost` 的 IPv6 解析导致连接超时、URL 数据库名替换误伤用户名——已在 conftest 修正（127.0.0.1 优先、只替换末段）。

## 未覆盖与限制

- 私有仓库需要配置 GITHUB_READ_TOKEN；匿名验收不覆盖私有访问路径。
- `simonw/ttok` 的 Review 与映射事件数据为 0，Review/事件的字段完整性主要依赖离线回放测试。
- 大仓库、超页上限、CI 日志下载与失败任务重跑尚未实测（日志按需下载属 M2+ 范围）。
- 工作区资源租约（README 7.1 的 workspace 在用标记）在分析型任务接入后启用；M1 的读路径为无锁只读。
- 未进行性能与质量评测；同步耗时只代表本机小仓库样本。

验收后服务保持运行（8100/3100），停止使用 `scripts/stop_local.ps1`。
