# ADR 0002：结构化输出的传输方式按提供商配置

状态：采用，2026-10-09。

M0 接入 DeepSeek（`deepseek-chat`）时发现：其 Chat Completions 接口拒绝 OpenAI 风格的 `response_format: {"type": "json_schema", "strict": true}`，返回 "This response_format type is unavailable now"。工具调用与 `tool_choice: "required"`、`max_tokens` 均正常，仅结构化输出的传输方式不兼容。

决策：新增配置 `LLM_STRUCTURED_OUTPUT`，取值 `json_schema`（默认，行为不变）或 `json_object`。`json_object` 模式下适配器发送 `response_format: {"type": "json_object"}`，并把输出 Schema 作为一条末尾 system 消息追加到请求提示词中（不改动调用方传入的消息列表）。DeepSeek 当前配置使用 `json_object`。

校验口径不变：不论传输方式，最终结果必须通过服务端 Pydantic Schema 校验与证据绑定校验，失败进入已有的单次修正流程；不存在把校验放宽的路径。该开关是显式的提供商能力配置（写在 .env，与 `LLM_TOKEN_PARAMETER` 同类），不是 ADR 0001 所述的"运行中偷偷改变调用协议"。

影响与边界：

- 适配器 payload 构造分支在 `CompatibleModel.complete`；协议（`ModelPort`/`ModelRequest`）不变。
- `json_object` 模式下 Schema 通过提示词传达，模型的遵守程度依赖其指令跟随能力；服务端校验是唯一验收标准。
- 真实模型能力探测（M0 probe）在两种模式下运行同一工具循环，只有通过校验才记录 `live_model_verified=true`。
- 未来接入原生支持 `json_schema` 的提供商时改回默认值即可，无需代码变更。
