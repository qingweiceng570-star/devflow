# 候选分析人工复核

状态：待人工复核。以下内容为模型输出，不代表已确认事实。

逐条对照完整证据填写同目录 reference-review.json 的 truth、citation_support 和 reason；原始内容均位于各案例 JSON 的 evidence 字段。

## simonw/ttok · issue-25

[完整输出与证据](issue-25.json)

目标提交：`df24f500721ce5f2a75d9ecfbd4fdc58a896c1e0`

### 发现 0 · info

目标 Issue #25 标题主张默认 tokenizer 应改为 GPT-5 所用编码；正文未给出具体编码名或实现方案，仅说明 0.4 已发布、该变更属破坏性变更，故计划发 0.5，并最终跳到 1.0。

引用：
- `b58bf04e-e709-463e-82cb-18bd32ad2ee4` · issue · [原始来源](None)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

### 发现 1 · info

Issue 当前状态为 closed，updated_at 为 2026-10-09T00:35:28Z；冻结证据中没有任何 PR、提交或发布记录可证明该变更已实现并发布。

引用：
- `b58bf04e-e709-463e-82cb-18bd32ad2ee4` · issue · [原始来源](None)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

### 发现 2 · low

历史上 #15 曾请求支持 gpt-4o 使用的 o200k_base 编码（依赖 tiktoken 0.7.0），说明默认/可选 tokenizer 演进是该仓库的既有主题，但 #15 与 #25 的具体诉求不同。

引用：
- `914c7543-898b-4d2c-91c3-39e8160d7c37` · issue · [原始来源](None)
- `b58bf04e-e709-463e-82cb-18bd32ad2ee4` · issue · [原始来源](None)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

### 发现 3 · low

Issue #18 指出工具缺少列出受支持模型的命令，默认编码变更会扩大“模型名到编码”映射，缺少该列表会增加用户困惑。

引用：
- `334d6606-de00-442b-a426-6a13d8ef2dc5` · issue · [原始来源](None)
- `b58bf04e-e709-463e-82cb-18bd32ad2ee4` · issue · [原始来源](None)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

## simonw/ttok · ci-37859163438

[完整输出与证据](ci-37859163438.json)

目标提交：`5a6ae1b7dc05a026085a25253fc27e2be5d5c0a2`

### 发现 0 · high

目标 run 37859163438（attempt 1，head_sha 5a6ae1b7dc05a026085a25253fc27e2be5d5c0a2）状态为 completed，结论为 failure。

引用：
- `2496f93c-b894-493e-9218-4462452894e1` · ci_run · [原始来源](None)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

### 发现 1 · high

在 5 个 job 中，只有 test (3.12)（job 113590523680）为 completed/failure；test (3.10)、test (3.11)、test (3.13)、test (3.14) 均为 completed/cancelled，需与 failure 区别对待，取消不等于出现错误。

引用：
- `2496f93c-b894-493e-9218-4462452894e1` · ci_run · [原始来源](None)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

### 发现 2 · medium

冻结证据中的 error_blocks 为空，且缺少 test (3.12) 的日志内容，因此没有任何证据指向失败的具体原因（代码、测试、依赖、环境或资源）。

引用：
- `2496f93c-b894-493e-9218-4462452894e1` · ci_run · [原始来源](None)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

### 发现 3 · info

快照声明本次为历史证据重放（FROZEN_EVIDENCE_ONLY），观察时间为 2026-10-26，run 更新时间 2026-10-08T23:23:46Z；因此该结论只反映冻结时刻，不代表当前状态。

引用：
- `2496f93c-b894-493e-9218-4462452894e1` · ci_run · [原始来源](None)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

## simonw/ttok · pr-16

[完整输出与证据](pr-16.json)

目标提交：`b56570be465aca1b95087be00b09c167c638a832`

### 发现 0 · low

在 --chunksize 下，若未同时指定 --tokens/--encode 且指定 --truncate，新增分支会优先于 truncate 分支执行，因此输出解码后的分块文本，而不沿用原有 nl=False 的截断输出路径。这属于行为差异，PR 描述也仅明确承诺 text/tokens/encode 三种模式。

引用：
- `0a354a93-03fc-4f42-b336-c2e764fe9413` · pr_diff · [原始来源](https://github.com/simonw/ttok/pull/16/files)
- `402c7ae3-c2f0-4476-b509-76804b5a0c84` · pull_request · [原始来源](https://github.com/simonw/ttok/pull/16)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

### 发现 1 · low

新增测试仅覆盖 --tokens、纯文本、--encode 三种分块模式，未覆盖 --chunksize 与 --truncate 的组合、chunksize 大于 token 数或为负值等边界。

引用：
- `f21cf2a8-fbf6-4e06-9d06-aee9882870cf` · pr_diff · [原始来源](https://github.com/simonw/ttok/pull/16/files)
- `0a354a93-03fc-4f42-b336-c2e764fe9413` · pr_diff · [原始来源](https://github.com/simonw/ttok/pull/16/files)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

## psf/requests · psf-requests-pr-6655

[完整输出与证据](psf-requests-pr-6655.json)

目标提交：`c0813a2d910ea6b4f8438b91d315b8d181302356`

### 发现 0 · medium

新的 _get_connection 复制了原 get_connection 的 proxy 解析逻辑，但仅对无代理路径有新增测试；代理+TLS 组合路径无对应测试证据。

引用：
- `9a93a369-55b0-5974-a911-7e74ab12a0a4` · pr_diff · [原始来源](https://github.com/psf/requests/pull/6655/files)
- `2f339132-0d40-5ab6-a85f-8ecb120dc97e` · pr_diff · [原始来源](https://github.com/psf/requests/pull/6655/files)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

### 发现 1 · low

_get_connection 用 ValueError 捕获并转 InvalidURL，而 send() 外层仍捕获 LocationValueError；两处异常映射并存，行为变化未在证据中体现为测试覆盖。

引用：
- `9a93a369-55b0-5974-a911-7e74ab12a0a4` · pr_diff · [原始来源](https://github.com/psf/requests/pull/6655/files)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

### 发现 2 · low

_urllib3_request_context 的 annotate 把返回类型写成字符串字面量 "(typing.Dict[str, typing.Any], typing.Dict[str, typing.Any])"，为无实际类型的字符串注解。

引用：
- `9a93a369-55b0-5974-a911-7e74ab12a0a4` · pr_diff · [原始来源](https://github.com/psf/requests/pull/6655/files)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。

### 发现 3 · info

tox.ini 将 pytest tests 改为 pytest {posargs:tests}，不传 posargs 时行为不变，属于小改动。

引用：
- `b9c70e66-1747-5791-8b43-df1bd80935a4` · pr_diff · [原始来源](https://github.com/psf/requests/pull/6655/files)

复核：事实支持 / 反驳 / 不确定；引用充分 / 部分 / 不支持。
