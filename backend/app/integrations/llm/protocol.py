from typing import Any, Protocol

from pydantic import Field

from app.domain.contracts import Contract


class ModelRequest(Contract):
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: str = "auto"
    output_schema: dict[str, Any] | None = None
    max_output_tokens: int = 1000


class ToolCall(Contract):
    id: str
    name: str
    arguments: str


class ModelReply(Contract):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    usage_estimated: bool = False
    finish_reason: str = "stop"
    provider_request_id: str | None = None


class ModelPort(Protocol):
    async def complete(self, request: ModelRequest) -> ModelReply: ...
