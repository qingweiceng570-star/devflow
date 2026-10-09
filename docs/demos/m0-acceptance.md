# M0 验收记录

日期：2026-10-08。范围：工程、身份、模型协议、只读样例工具循环与本地界面。

用户已选择先交付通过离线验收的 M0，稍后配置真实模型。当前没有外部模型密钥，也没有执行实际 GitHub 请求。

| 检查 | 结果 | 证据与限制 |
| --- | --- | --- |
| 后端自动测试 | 45 passed | 认证/会话/CSRF/限流、工具参数与 scope、循环/预算/取消/超时、协议与引用校验；模型初始化失败释放锁 |
| 代码检查 | passed | Ruff；覆盖 app、tests 与 migrations |
| PostgreSQL 初始迁移 | passed | 0001_m0 为 head；实际数据库执行迁移 |
| PostgreSQL Schema 对照 | passed | alembic check：No new upgrade operations detected |
| 前端 TypeScript | passed | pnpm typecheck |
| 前端生产构建 | passed | Next.js 16.4.0；首页、登录与显式 API 转发 |
| 浏览器完整流程 | passed | 登录 → fixture → 证据值 → 刷新记录 → 退出 → 未登录重定向 |
| 移动端错误/布局 | passed | 390×844，无横向溢出；错误密码提示 |
| Worker | passed | 实际心跳写入数据库，面板可观察在线 |
| 启动/停止脚本 | passed | 核对 PID 与启动时间，关闭本项目进程树，再启动成功 |
| 模型 HTTP 兼容协议 | passed（模拟上游） | MockTransport 核对 endpoint、tools、strict schema、错误脱敏；不冒充真实服务 |
| 真实模型工具/结构能力 | 待配置 | BASE_URL / MODEL / API_KEY 为空；用户选择后续接入 |
| Docker 数据库 | passed | 独立 devflow Compose 名称、固定 PostgreSQL digest、健康检查 |
| Docker 全应用 | passed | app profile 实际构建；迁移成功退出；API/Worker/Web 启动；ready 与 login 返回 200；两项 E2E 通过 |

本轮存在 Starlette 测试客户端对 httpx 的弃用提示，测试均通过；未来升级时按上游兼容性再处理，不影响当前适配器对 httpx 的使用。没有进行性能或回答质量评测。

首次 Docker 构建遇到 Docker Hub 授权端点超时；分别重试固定 Python/Node 基础镜像下载后完成构建，没有修改系统 DNS 或 Docker 全局配置。验收后停止 Docker API/Worker/Web，恢复本机后台进程；数据库容器保留。

M0 的 ProbeAnswer 用于协议探测，绑定实际工具返回的证据 ID 和 verification_value。离线模型有合成用量，界面明确标记。真实模型未配置时返回 unavailable，不自动换成 fixture。

业务 Run/Job、GitHub API、RAG 和写操作尚未实现。持久化探测记录不等同于持久化业务工作流；M0 Worker 心跳也不等同于已建任务队列。

## 2026-10-09 补记：真实模型接入

DeepSeek（`deepseek-chat`，OpenAI 兼容接口）已通过项目根 .env 配置接入。其接口拒绝 `response_format: json_schema`，结构化输出改用 `LLM_STRUCTURED_OUTPUT=json_object`（Schema 由适配器追加到提示词，服务端 Pydantic 与证据绑定校验不变），见 [ADR 0002](../adr/0002-structured-output-transport.md)。

`normal_pr`、`risk_pr`、`failed_ci` 三个样例的真实模型探测全部通过：工具调用、结果回传、结构化输出、证据绑定四项均为 true，`live_model_verified=true`；单例约 2 秒、约 1000 Token，usage 来自提供商返回值（非估算）。登录后的验证面板与 `GET /api/v1/settings/capabilities` 均显示最近探测为 passed。

探测是有界诊断请求，仍不代表已分析真实仓库；GitHub 接入与业务分析从 M1 开始。
