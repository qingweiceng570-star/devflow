# ADR 0003：M1 同步、队列与工作区的边界选择

状态：采用，2026-10-09。

M1 实现 GitHub 只读同步、持久化 Job 队列、固定 SHA 代码工作区与证据采集。以下为实现层的关键选择，均不改变 README 的既定契约（D01–D12）。

## 同步边界（README 8.2 的具体化）

- 每个资源每次运行最多 `SYNC_MAX_PAGES`（默认 4）页；Open Issue/PR 全量扫描与 closed 增量扫描共享该预算。达到上限时该资源标记 `capped`，运行整体为 `partial`，且**不推进 watermark**，下次运行从头补扫（upsert 幂等）。
- 首次接入窗口：Issue/PR closed 部分默认 90 天、CI 默认 30 天；增量运行时改用 `committed_watermark - SYNC_OVERLAP_MINUTES` 作为起点，处理时钟精度与更新延迟。
- 列表第一页使用 ETag 条件请求：304 表示内容未变，按"零新增"完成。closed 扫描按 updated 降序在越过 cutoff 后提前结束。
- Review 仅针对范围内 PR（默认最多 10 个）读取；CI jobs 仅针对 head SHA 命中已知 PR 的运行（默认最多 5 个），其余按需在后续阶段读取。
- 原始载荷按内容哈希存入 `var/objects` 并登记 `source_snapshots`（同 revision 同哈希去重）；文本正文与快照分离存储。

## 失败、限流与重试（README 17.5 的具体化）

- 网络错误与 5xx 在 HTTP 客户端内做有限重试；配额型 403/429 不内联等待，映射为 `SOURCE_RATE_LIMITED(retry_after)`，由队列按 `retry_after` 调度（受 `JOB_RETRY_MAX_DELAY_SECONDS`=900s 上限约束）。
- 单资源不可重试错误（如无权限）记录进 `sync_runs.errors` 并继续其余资源；可重试错误中止运行并把 `sync_run` 置回 `queued` 等待 Job 重试；Job 重试耗尽后由恢复流程把运行置为 `failed`。
- 每次执行尝试重置 `counts/errors`（运行描述"最近一次尝试"）；历史痕迹保留在 `jobs.last_error` 与逐资源已提交的页级数据中。

## 队列与租约（README 17 的具体化）

- PostgreSQL 表队列，`FOR UPDATE SKIP LOCKED` 短事务领取；SQLite 测试运行相同的状态机（锁语义为空操作），真实并发行为由 `@pytest.mark.pg` 的集成测试在真实 PostgreSQL 上验证。
- 完成/失败一律携带 `lease_epoch` 条件更新；过期 worker 的提交与旧 epoch 写入被拒绝（验收矩阵 A27）。
- 资源租约 `(repo, kind, key)` 带过期时间：运行期间在每个页面边界续期，同资源并发运行互相跳过（记 `resource_busy`）。

## 工作区与网络出口（README 9.1 的具体化）

- 使用系统 git CLI（subprocess、无 shell、argv 传参）；凭据与代理通过 `GIT_CONFIG_*` 环境变量注入（`http.extraheader`、`http(s).proxy`），不进入命令行、URL、`.git/config` 或日志。
- 每个仓库一个 bare mirror（`var/workspaces/<repo_id>/git`）；读取一律固定在显式 SHA；缺失 SHA 通过 `git fetch origin <sha>` 获取（GitHub 对 PR head 可达 SHA 提供该能力），失败返回 `CODE_UNAVAILABLE`。
- 不启用 hooks、子模块、LFS 自动下载与过滤器；路径策略拒绝绝对路径、盘符、UNC、`..`、NUL、NTFS ADS 与 Windows 折叠歧义片段。

## 网络出口默认值（本机问题驱动的决定）

`GITHUB_TRUST_ENV` 默认 `false`：httpx 默认读取 Windows 系统代理，实测共享代理出口 IP 的 GitHub 匿名配额长期耗尽（403 rate limit），而直连出口正常。API 请求因此默认直连；git 操作通过独立的 `GITHUB_GIT_PROXY`（示例 `http://127.0.0.1:7897`）走本机代理工具。两者都是显式配置，不静默改变出口；LLM 适配器不受影响（保持默认行为）。
