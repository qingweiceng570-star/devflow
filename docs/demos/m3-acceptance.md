# M3 验收记录

日期：2026-10-09。范围：文档上传与结构感知切分、BM25/向量混合检索（RRF）、索引代与 Outbox、召回测试页、`search_knowledge` 工具与知识问答。

测试仓库：`simonw/ttok`。文档：仓库真实 `README.md`（8.4 KiB）与 `LICENSE`（11.4 KiB），经 API 上传。模型：DeepSeek `deepseek-chat`（真实调用）。向量通道：未配置嵌入服务，按设计以 BM25 单路运行并显式标记。

## 实测结果（浏览器 + API 全流程）

| 检查 | 结果 | 证据与限制 |
| --- | --- | --- |
| 文档上传与自动索引 | passed | 两份真实文档经 multipart 上传；Worker 消费 index job 在约 2 秒内构建并激活索引代（**35 块**，`embedded=false, degraded_reason=not_configured`）；同内容重传返回 `changed=false` 不产生新版本 |
| 召回测试（检索页） | passed | 查询「如何按 token 数量截断文本」→ 6 条结果，命中 README 的 `--help` 段落与截断说明；trace 完整：BM25 候选 7、向量通道 `not_configured`、degraded 为空、索引代与过滤淘汰数可见（截图 var/m3-recall.png） |
| 删除即时过滤 | passed | 上传含唯一标记的临时文档 → 召回命中（score 0.0164）→ 删除后**重建前**再查：0 命中且 `dropped_by_filtering=1`（DB 回查权威，与 generation 无关） |
| 知识问答闭环 | passed | 问答运行 `0d0ab5f7`：completed，coverage=complete，**5 条带引用的发现、7 处引用**；13,141 真实 token / 2 步 / 5 次工具调用（search_knowledge×2 + search_code×2 + get_repo_summary），全部成功 |
| 答案质量（混合取证） | passed | 答案交叉验证：README 文档说明 + `--help` 片段 + **当前 head（df24f500）ttok/cli.py:124-125 的 `tokens[:truncate]` 实现**；并做负向验证（代码库中不存在 `--max-tokens` 字符串），指出 README 未提及该选项——正是 README 11.5 的取证路由 |
| 降级路径 | passed | 向量通道 `not_configured` 在页面以「未启用（BM25 单路可用）」呈现；离线测试覆盖单路降级与双路失败（`RETRIEVAL_UNAVAILABLE`）；Milvus 为可选 profile（pymilvus 可选依赖组），本机未启用嵌入/向量服务，向量通道的真实链路未实测 |
| 后端测试 | passed | 189 passed（新增 38：分词 5、切分 6、BM25 5、检索 7、索引生命周期 6、文档 API 3、知识 API 3、知识问答运行 3） |
| 代码检查 / 迁移 | passed | Ruff 干净；`alembic check` 一致；就绪检查改为从迁移文件推导 head（修复了两次同源 503） |
| 前端 | passed | typecheck + build（新增 /knowledge 页与会话模式选择）；Playwright 4 passed |

截图：[知识库与召回测试](../../var/m3-recall.png)、[知识问答结果](../../var/m3-qa.png)、[问答引用弹层](../../var/m3-qa-evidence.png)。

## 验收中发现的真实问题（已修复并回归）

1. **预算预留把字符当 token**：首次知识问答被误报 `budget_exhausted`（真实消耗 2,960 / 30,000）——循环预留用 JSON 长度 1:1 估算，检索型会话两轮工具结果即击穿。修复：预留改用 chars/2 共享估算器；同时收窄 `search_knowledge` 模型可见载荷（每块 1200 字符、去掉完整 trace）。
2. **激活顺序与部分唯一索引**：ORM flush 顺序不保证「先降级旧代再激活新代」，个别路径触发唯一约束。修复为显式两条 UPDATE（测试覆盖）。
3. **就绪版本号滞后**：M2/M3 两次因手写期望迁移版本导致 503。修复：从 alembic 迁移文件推导 head。
4. **瞬时 git 抖动**：一次 `search_code` 因工作区拉取瞬时失败返回 `CODE_UNAVAILABLE`（可重试），下一轮运行正常；工具错误面保持稳定码，运行不崩溃。

## 未覆盖与限制

- 向量通道的真实 Milvus+嵌入链路未在本机实测（无嵌入服务）；融合/降级逻辑由 Fake 通道测试覆盖，Milvus 存储实现待有嵌入服务时按 knowledge profile 验证。
- BM25 中英文质量未经标注集评测（README 13.2 明确不预设优于其他分词器）；召回测试页提供了校准入口。
- 索引增量复用、检索结果缓存、Rerank 模型（RERANK_MODEL 预留位）未实现；会话级上下文压缩仍是基础裁剪（M6 范围）。
- 召回测试页无分页；文档列表上限 50 条/页。
