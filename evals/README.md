# 评测数据与运行方式

本仓库保留两类基线，不能将两者的通过率混为模型准确率。

| 数据 | 内容 | 执行 | 结果 |
| --- | --- | --- | --- |
| `dataset-v1.json` | 40 个合成规则、协议、检索边界用例 | `scripts/evaluate.py` | `results/offline-latest.json` |
| `real-v1/` | 已授权公开仓库 simonw/ttok 的三个真实分析及原始证据快照 | `scripts/evaluate_real.py` | 冻结基线 `results/real-baseline-v1.json`；重算输出 `results/real-latest.json` |
| `public-pr-v2/` | psf/requests PR #6655 的固定 head、PR 原文与完整 API 补丁 | `scripts/eval_candidate.py capture` | 只含输入证据，不含标准答案 |
| `candidate-20261026/` | 两个公开仓库、四个案例，当前提示词的模型重放结果 | `scripts/eval_candidate.py replay` | `results/candidate-20261026.json`，14 条发现待人工复核 |

## 真实基线 v1

目标：Issue #25、CI 37859163438 attempt 1、PR #16。三个模型运行均来自此前已授权的 DeepSeek 分析，本次评分不调用模型。所有案例是开发集，来自一个仓库，不能推断其他项目的质量。

`manifest.json` 固定案例文件 SHA-256；每个案例保留模型配置、耗时、记录用量、交付结果、完整引用内容与内容哈希，以及当时准备的目标工具结果。原始结果保留，不能用新提示词改写旧基线。`export_real_cases.py` 拒绝覆盖已冻结的 real-v1。

`reference-review.json` 逐条记录 finding 序号、事实判断、引用支持度及原因，绑定输出哈希。当前复核者为 assistant_source_review，人工复核状态为 pending；这些数值是来源复核初稿，不是人工认证金标准。

### 评分规则

- `supported`：保存的来源或目标上下文直接支持事实，推断边界明确。
- `refuted`：控制流或原文与所报事实矛盾。
- `uncertain`：现有来源不足以判定；保留该项，不擅自判为正确。
- 事实正确比例：supported / (supported + refuted)；未判定项单独报告。
- 已报告发现中的误报占比：refuted / (supported + refuted)。报告键为 false_discovery_rate；由于没有未报告的真阴性集合，不计算通常定义的 false-positive rate。
- 引用完整性：引用 ID 存在且内容哈希一致。这不证明引用支持结论。
- 引用充分支持比例：逐条复核为 citation_support=supported 的 finding 数 / 全部 finding 数。
- 裁决符合率：最终 verdict 与参考裁决一致；不能代替细节正确率。
- 召回率：暂无独立穷尽缺陷集，明确记录 null。

初始结果：10 条 finding 中，6 条支持、2 条反驳、2 条未判定；已判定项事实正确比例 75%，误报占比 25%；充分引用支持比例 30%；三个保守裁决均符合参考裁决。中位运行耗时 15.28 秒，累计历史记录用量 26,138 tokens。没有价格配置，未推算费用。

典型问题：PR #16 的旧分析声称 chunksize=0/负数会抛 range ValueError；实际零值被 if 条件跳过，负步长为空迭代。另将 --encode 输出的一行一 chunk 承诺误套到解码文本。CI 有三条真实事实却引用了不包含 jobs 的运行元数据，暴露出引用支持度不足。

### 重复运行与后续对比

项目根目录：

```powershell
backend/.venv/Scripts/python.exe -X utf8 scripts/evaluate.py
backend/.venv/Scripts/python.exe -X utf8 scripts/evaluate_real.py
```

评分会拒绝被篡改的案例或与输出不匹配的复核记录。数据集、复核记录和评分器哈希一并写入报告。

新增真实案例时，使用新版本目录，保留固定提交、run/attempt、查询覆盖、来源哈希、模型/提示词配置和用量。先依据来源编写参考判断，再查看候选答案；新答案逐条复核，不能沿用旧 finding 序号的标签。正式回归集应来自尚未参与提示词调整的其他项目，由人工独立复核；缺少完整缺陷集时继续保持 recall=null。

本轮的 PR 提示词 v2 和 job 独立证据属于针对已发现问题的实现改进；没有新增模型运行，尚未给出改进后模型精度提升的结论。

## 2026-10-26 候选评测与复核流程

已使用当前配置的 DeepSeek 对冻结输入重放四个案例，模型请求 8 次，记录用量 34,455 tokens。每次都固定输入证据、目标 SHA、提示词哈希和来源案例哈希，候选输出写入新目录，不覆盖原基线。

这是固定证据与当前提示词的测试，不覆盖实时 GitHub 取证、全工具集合或生产裁决策略。CI 仍使用旧快照，因此也能暴露缺少 job 独立引用的限制。两个仓库、四个开发案例不足以推断泛化质量；没有独立完整缺陷集，召回率仍为 null。

复核者逐条阅读 `output.findings` 与 `evidence`，在 `reference-review.json` 中填写 `truth`（supported/refuted/uncertain）、`citation_support`（supported/partial/unsupported）和具体理由，并填写参考裁决。缺少资料应判 uncertain，尚未复核保持 pending。只有人工实际完成后，才将 `reviewer_type` 改为 human、`human_review_status` 改为 completed。修改答案会使已有复核哈希失效；禁止复用旧 finding 序号对应的标签。

```powershell
# 重算当前候选；pending 指标保持 null，不把它当作通过率。
backend/.venv/Scripts/python.exe -X utf8 scripts/evaluate_real.py --dataset evals/candidate-20261026 --output evals/results/candidate-20261026.json --min-repositories 2

# 正式验收门槛：未完成人工复核时返回非零退出码。
backend/.venv/Scripts/python.exe -X utf8 scripts/evaluate_real.py --dataset evals/candidate-20261026 --output var/eval-human-gate.json --min-repositories 2 --require-human-review

# 新一轮默认只预览案例/模型/预算；添加 --live 才实际请求已配置模型。
backend/.venv/Scripts/python.exe -X utf8 scripts/eval_candidate.py replay --dataset evals/real-v1 --dataset evals/public-pr-v2 --output var/eval-candidate-new --max-total-tokens 60000
```

采集新案例用 `eval_candidate.py capture --repository owner/repo --pr N --output 新目录`。采集器只接受公开仓库，拒绝缺失补丁、超出一页或采集中 head 变化的 PR。先冻结独立的预期事实/缺陷清单，再运行候选模型；当前 pending 表用于逐发现复核，不自动成为独立金标准。
