# M2 验收记录

日期：2026-10-09。范围：RunService 生命周期、通用 Agent Loop、受控工具集（PR 审查九件套）、裁决规则、SSE 与聊天/运行前端。

测试仓库：`simonw/ttok`（同 M1）；目标：开放中的 PR #16（--chunksize），固定 head `b56570be`。模型：DeepSeek `deepseek-chat`（真实调用）。

## 实测结果（浏览器全流程）

| 检查 | 结果 | 证据与限制 |
| --- | --- | --- |
| 端到端审查 | passed | 浏览器完成：登录 → 仓库总览 → 新建会话 → 提交 PR #16 → SSE 进度 → 终态渲染。运行 `eaf5d88c`：**completed**，verdict=insufficient_evidence（无必需检查配置时的正确降级），coverage=complete，freshness=fresh |
| 结构化结论质量 | passed | 4 条带证据的发现（2 中 2 低）：`chunksize=0` 会抛 ValueError 未做参数校验（ttok/cli.py:118-119）、chunksize 分支无回归测试、`--chunksize` 与 `--truncate` 交互未定义（cli.py:130-137）、测试覆盖缺口（tests/test_ttok.py:64-66） |
| 证据纪律 | passed | 5 条证据绑定（3 collected + 2 cited）；发现全部引用本次运行收集的证据；缺口与局限性如实列出（未读 cli.py 全文、头版本 CI 未验证、必需检查未配置） |
| 用量与轨迹 | passed | 真实用量 9,347 token（非估算），2 次模型决策、3 次工具调用（get_pr / get_pr_diff / get_ci_runs），全部落库（agent_runs/tool_calls/run_events） |
| 刷新恢复 | passed | 运行结束后刷新页面从 `GET /runs/{id}` 恢复完整结果；SSE 断线重连按 seq 去重 |
| 取消 / 预算 / head 变化 / 大 diff / 伪造引用 | passed（离线回放） | 12 个分析管线测试覆盖：步间取消、预算耗尽→completed(partial)+insufficient、输出前 head 复查→stale+stop_reason=head_changed、diff 超限→partial+未审查清单、伪造引用→剥离降级不致命、工具字节预算 |
| API 契约 | passed | 会话创建/消息分页/运行提交（幂等键重放、ACTIVE_RUN_EXISTS 409）/运行详情/取消/工具轨迹/SSE 重放与 `after` 恢复（7 个契约测试） |
| 后端测试 | passed | 150 passed（新增 35：分析管线 13、运行生命周期 7、API/SSE 7、裁决规则 7、探针截断修复 1） |
| 代码检查 / 前端 | passed | Ruff 干净；`pnpm typecheck`、`pnpm build`（新增仓库总览与会话两页）；Playwright 4 passed |
| 契约导出 | passed | openapi.json 含 10 个 M2 路由；domain-schemas.json 含 Finding/Hypothesis/MissingEvidence/NextAction/PRReviewDraft |

截图：[会话与结果](../../var/m2-chat-result.png)、[运行中进度](../../var/m2-run-progress.png)、[证据详情弹层](../../var/m2-evidence.png)、[仓库总览](../../var/m2-repo-overview.png)。

## 验收中发现的真实问题（已修复并回归）

1. **同证据多发现导致崩溃**：两条发现引用同一 evidence_id 时 cited 绑定重复插入，触发唯一约束 → INTERNAL_ERROR，运行卡在 running。修复：按 evidence_id 去重绑定；并加处理器兜底（未预期异常也保证运行终态）。新增回归测试。
2. **终稿截断**：首次真实运行在输出上限（当时 1000 token）处被截断（MODEL_OUTPUT_INCOMPLETE）。修复：上限提至 3000；循环对 `finish_reason=length` 先尝试解析，失败给一次"精简重写"机会。新增两个回归测试。
3. **瞬时上游断连**：一次运行在首次模型调用即 MODEL_UNAVAILABLE（代理节点抖动），重发后正常。按 ADR 0004，分析不做自动重放，用户重新发起。

## 未覆盖与限制

- 必需检查的完整通过路径（recommend_merge 保持不改判）用离线回放验证，未在真实仓库配置 required_checks 复现。
- 多轮会话的上下文压缩只有基础裁剪（按字符估算、超限丢最旧）；会话摘要与逐级压缩是 M6 范围。
- SSE 未做代理层（nginx 等）以外的断网直播验证；事件保留策略尚未实现（无过期清理路径）。
- 真实仓库样本仍为小型仓库；大 diff 与并发多会话的现场压测未做。
