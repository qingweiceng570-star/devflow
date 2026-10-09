# ADR 0004：M2 运行链路与流式的实现选择

状态：采用，2026-10-09。

M2 交付首个业务分析闭环（PR 审查）：RunService 生命周期、手写 Agent Loop 的通用化、受控工具集、裁决规则、SSE 与聊天前端。以下为关键实现决策。

## 运行与重试

- 分析类 Job `max_attempts=1`：分析不可断点续跑，被中断或失败即终态（`WORKER_INTERRUPTED`/具体错误），用户重新发起新 Run。瞬时上游错误（如模型瞬断）同样以失败终止，不做自动重放——单次运行会消耗模型预算，重放成本由用户显式决定。
- `runs` 表对会话持有部分唯一索引（queued/running/recovering 唯一），提交与消息、Job、`run.queued` 事件同事务；取消是"标记后由执行方在步间检查"，终态迁移用条件更新裁决竞态（取消先行则完成提交丢失，反之亦然）。
- 处理器最外层有兜底：未预期异常也会把运行置为失败终态，避免永久 running 记录（验收中真实触发过一次，见 m2-acceptance）。

## 通用 Agent Loop（agents/runtime.py）

- 探针（agents/probe.py）与业务 Agent 共用同一循环实现，通过 `LoopPolicy` 保留探针"工具必调至有证据 / 有证据后才要求 schema / 有证据后不再提供工具"的既有线形，M0 验收行为不变。
- `finish_reason=length` 的终稿先尝试解析：可解析则接受；不可解析时给一次"更精简重写"的修复机会（与 schema 修复共享一次上限），两次都截断才 `MODEL_OUTPUT_INCOMPLETE`。默认输出上限从 1000 提至 3000（验收中真实触发过截断）。
- 预算账本（budget_accounts）在 M2 为单写者模型；并行工作流所需的预留/结算表（budget_reservations）随 M5 引入。

## 工具与证据（tools/）

- 工具输入一律 Pydantic 校验；repo/SHA/凭据来自 ExecutionContext，模型只能提供业务参数。工具错误返回稳定 ToolResult 错误码，绝不抛出到循环外（除非契约性错误）。
- 模型可见工具结果双重封顶：单次 64 KiB（tool_result_max_bytes）、每运行累计 256 KiB（tool_bytes_total_limit，超限返回 `TOOL_BUDGET_EXCEEDED` 提示收束）。这是 M6 完整上下文预算器之前的临时边界，配置可调。
- PR 差异在运行开始时一次性准备：全量文件清单 + 优先路径排序 + 每文件补丁截断 5000 字符；每文件一条证据；超过 200 文件或 2 MiB 变更正文的文件进入"未审查清单"并强制 coverage=partial。`get_pr_diff` 只分页回放已准备的差异，不重新全量拉取。

## 裁决与引用纪律（domain/policies.py）

- 模型给出 verdict 建议，服务端按事实（阻塞级发现、覆盖度、新鲜度、草稿、mergeable、必需检查）降级，永不升级；正结论需全部条件可验证为真。
- 必需检查来源：仓库配置 `required_checks`（管理员通过 PATCH /repos 写入）；未配置时任何 recommend_merge 都会降级为 insufficient_evidence。
- 引用过滤：不在本运行收集集合内的 evidence_id 一律剥除；整条发现失去全部引用时降入局限性，运行不因此失败（README 9.3 的"退回一次修正，仍失败则删除无效引用"以确定性方式实现）。

## SSE 与前端代理

- 事件存于 run_events（run 内自增 seq，唯一约束），SSE 端点按 `Last-Event-ID`/`after` 重放后跟踪到终态；15 秒心跳注释；事件未清理前无 410 路径（保留策略随存储治理引入）。
- Next.js 代理对 `Accept: text/event-stream` 请求透传流式响应并取消 65 秒缓冲超时；其余请求维持有界缓冲。浏览器用 EventSource（同名事件逐类注册），刷新页面以 `GET /runs/{id}` 为准恢复。
