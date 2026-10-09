import json

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import DomainError
from app.integrations.llm.protocol import ModelReply, ModelRequest, ToolCall


class CompatibleModel:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings, self.transport = settings, transport

    async def complete(self, request: ModelRequest) -> ModelReply:
        if not self.settings.model_configured:
            raise DomainError("CAPABILITY_UNAVAILABLE", "请在 .env 配置模型地址、名称和密钥", 503)
        payload = {
            "model": self.settings.llm_model,
            "messages": request.messages,
            self.settings.llm_token_parameter: request.max_output_tokens,
            "stream": False,
        }
        if request.tools:
            payload.update(tools=request.tools, tool_choice=request.tool_choice)
        if request.output_schema:
            if self.settings.llm_structured_output == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "capability_answer",
                        "strict": True,
                        "schema": request.output_schema,
                    },
                }
            else:
                # Providers without json_schema support still need the schema in the prompt;
                # server-side Pydantic validation remains the enforcement either way.
                payload["response_format"] = {"type": "json_object"}
                payload["messages"] = [
                    *request.messages,
                    {
                        "role": "system",
                        "content": "Return only a single JSON object conforming to this JSON schema: "
                        + json.dumps(request.output_schema, ensure_ascii=False),
                    },
                ]
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.model_timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    self.settings.llm_base_url + "/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": "Bearer " + self.settings.llm_api_key.get_secret_value()
                    },
                )
                if response.status_code in {401, 403}:
                    raise DomainError(
                        "MODEL_AUTH_FAILED", "模型服务拒绝访问，请检查服务端配置", 503
                    )
                if response.status_code == 429:
                    raise DomainError("MODEL_RATE_LIMITED", "模型服务暂时限流", 429, retryable=True)
                if response.status_code >= 400:
                    raise DomainError(
                        "MODEL_REQUEST_REJECTED", "模型服务拒绝请求；请检查模型及协议兼容性", 502
                    )
                if len(response.content) > 1024 * 1024:
                    raise DomainError("MODEL_RESPONSE_TOO_LARGE", "模型响应超过 1 MiB", 502)
                body = response.json()
                choice = body["choices"][0]
                message = choice["message"]
                calls = [
                    ToolCall(
                        id=call["id"],
                        name=call["function"]["name"],
                        arguments=call["function"]["arguments"],
                    )
                    for call in message.get("tool_calls", [])
                    if call.get("type") == "function"
                ]
                if len(calls) != len(message.get("tool_calls", [])):
                    raise ValueError("Unsupported tool call type")
                usage = body.get("usage") or {}
                estimated = "prompt_tokens" not in usage or "completion_tokens" not in usage
                return ModelReply(
                    content=message.get("content"),
                    tool_calls=calls,
                    finish_reason=choice.get("finish_reason", "stop"),
                    input_tokens=usage.get(
                        "prompt_tokens",
                        len(json.dumps(request.messages, ensure_ascii=False)) // 2 + 1,
                    ),
                    output_tokens=usage.get(
                        "completion_tokens", len(json.dumps(message, ensure_ascii=False)) // 2 + 1
                    ),
                    usage_estimated=estimated,
                    provider_request_id=response.headers.get("x-request-id"),
                )
        except httpx.TimeoutException:
            raise DomainError("MODEL_TIMEOUT", "模型请求超时", 504, retryable=True) from None
        except httpx.RequestError:
            raise DomainError(
                "MODEL_UNAVAILABLE", "无法连接模型服务", 503, retryable=True
            ) from None
        except (KeyError, IndexError, TypeError, ValueError, ValidationError):
            raise DomainError("MODEL_PROTOCOL_ERROR", "模型响应不符合兼容接口格式", 502) from None
