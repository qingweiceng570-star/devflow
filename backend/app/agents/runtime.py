"""Hand-written agent loop (README 10.2).

One implementation of the bounded model/tool iteration shared by the probe
diagnostic and business agents: native tool calling, schema-validated final
output with a single bounded repair, repeat-call policy, budget reservation
and explicit stop conditions (step/tool/time/budget/cancel).
"""

import asyncio
import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from app.context.budgeter import estimate_tokens
from app.core.config import Settings
from app.core.errors import DomainError
from app.domain.contracts import ToolResult
from app.integrations.llm.protocol import ModelPort, ModelRequest, ToolCall


class ToolsPort(Protocol):
    @property
    def schemas(self) -> list[dict]: ...

    async def execute(self, name: str, arguments: str) -> ToolResult: ...


def _tools_have_evidence(tools: ToolsPort) -> bool:
    return bool(getattr(tools, "has_evidence", False))


@dataclass
class LoopPolicy:
    """Provider/agent-facing loop switches; defaults suit business agents."""

    # "required_until_evidence": force a tool call until one succeeded (probe).
    tool_choice: Literal["auto", "required_until_evidence"] = "auto"
    # "after_evidence": only request structured output once evidence exists.
    schema: Literal["always", "after_evidence"] = "always"
    # Probe keeps its accepted wire shape: no tools offered after evidence exists.
    drop_tools_after_evidence: bool = False


@dataclass
class LoopHooks:
    """Persistence/event/stop callbacks; the loop itself stays side-effect free."""

    check_stop: Callable[[], None] | None = None
    reserve_budget: Callable[[int], None] | None = None
    settle_budget: Callable[[int], None] | None = None
    on_model_step: Callable[[dict], None] | None = None
    on_tool_started: Callable[[ToolCall, UUID], None] | None = None
    on_tool_finished: Callable[[ToolCall, UUID, ToolResult, int], None] | None = None


@dataclass
class LoopOutcome:
    parsed: BaseModel
    content: str
    trace: list[dict] = field(default_factory=list)
    total_tokens: int = 0
    usage_estimated: bool = False
    model_steps: int = 0
    tool_calls: int = 0


class AgentLoop:
    def __init__(
        self,
        model: ModelPort,
        tools: ToolsPort,
        settings: Settings,
        *,
        output_model: type[BaseModel],
        policy: LoopPolicy | None = None,
        hooks: LoopHooks | None = None,
        validate_output: Callable[[BaseModel], None] | None = None,
        cancel: asyncio.Event | None = None,
        timeout_seconds: float | None = None,
    ):
        self.model = model
        self.tools = tools
        self.settings = settings
        self.output_model = output_model
        self.policy = policy or LoopPolicy()
        self.hooks = hooks or LoopHooks()
        self.validate_output = validate_output
        self.cancel = cancel
        self.timeout_seconds = timeout_seconds or settings.agent_timeout_seconds

    def _check_stop(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise DomainError("CANCELLED", "任务已取消", 409)
        if self.hooks.check_stop is not None:
            self.hooks.check_stop()

    async def run(self, messages: list[dict]) -> LoopOutcome:
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        messages = list(messages)
        trace: list[dict] = []
        repeated: Counter = Counter()
        tokens, tool_count, repairs, estimated = 0, 0, 0, False

        for step in range(self.settings.agent_max_steps):
            self._check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DomainError("AGENT_TIMEOUT", "工具循环超时", 504)
            # Conservative upper-bound reservation using the shared estimator
            # (chars/2): raw JSON length would overshoot ~2x for content-heavy
            # tool results and falsely exhaust the budget.
            estimate = estimate_tokens(json.dumps(messages, ensure_ascii=False)) + estimate_tokens(
                json.dumps(self.tools.schemas, ensure_ascii=False)
            )
            reservation = estimate + self.settings.model_output_token_limit
            if self.hooks.reserve_budget is not None:
                self.hooks.reserve_budget(reservation)
            elif tokens + reservation > self.settings.agent_total_token_budget:
                raise DomainError("BUDGET_EXCEEDED", "模型调用累计预算不足", 409)

            has_evidence = _tools_have_evidence(self.tools)
            tool_choice = "auto"
            if self.policy.tool_choice == "required_until_evidence" and not has_evidence:
                tool_choice = "required"
            output_schema = None
            if self.policy.schema == "always" or has_evidence:
                output_schema = self.output_model.model_json_schema()
            tools_payload = self.tools.schemas
            if self.policy.drop_tools_after_evidence and has_evidence:
                tools_payload = []
            request = ModelRequest(
                messages=messages,
                tools=tools_payload,
                tool_choice=tool_choice,
                output_schema=output_schema,
                max_output_tokens=self.settings.model_output_token_limit,
            )
            try:
                reply = await asyncio.wait_for(self.model.complete(request), remaining)
            except TimeoutError:
                raise DomainError("AGENT_TIMEOUT", "工具循环超时", 504) from None
            self._check_stop()
            tokens += reply.input_tokens + reply.output_tokens
            estimated |= reply.usage_estimated
            if self.hooks.settle_budget is not None:
                self.hooks.settle_budget(reply.input_tokens + reply.output_tokens)
            elif tokens > self.settings.agent_total_token_budget:
                raise DomainError("BUDGET_EXCEEDED", "模型服务报告用量超过预算", 409)

            entry = {
                "step": step + 1,
                "event": "model.completed",
                "tool_count": len(reply.tool_calls),
                "finish_reason": reply.finish_reason,
            }
            trace.append(entry)
            if self.hooks.on_model_step is not None:
                self.hooks.on_model_step(entry)
            if reply.finish_reason == "content_filter":
                raise DomainError("MODEL_OUTPUT_INCOMPLETE", "模型输出被截断或过滤", 502)
            if reply.finish_reason == "length":
                # A truncated reply is only fatal if it cannot be repaired into
                # a valid final answer within the single repair allowance.
                parsed_truncated = self._parse_output(reply, trace) if reply.content else None
                if parsed_truncated is not None:
                    return LoopOutcome(
                        parsed=parsed_truncated,
                        content=reply.content or "",
                        trace=trace,
                        total_tokens=tokens,
                        usage_estimated=estimated,
                        model_steps=step + 1,
                        tool_calls=tool_count,
                    )
                if repairs < 1:
                    repairs += 1
                    messages.append({"role": "assistant", "content": reply.content or ""})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "上一次输出因长度被截断。请用更精简的篇幅重新输出完整的 JSON："
                                "缩短 summary 与各条文本，只输出一个 JSON 对象。"
                            ),
                        }
                    )
                    continue
                raise DomainError("MODEL_OUTPUT_INCOMPLETE", "模型输出被截断或过滤", 502)

            if reply.tool_calls:
                ids = [call.id for call in reply.tool_calls]
                if len(ids) != len(set(ids)) or not all(ids):
                    raise DomainError("MODEL_PROTOCOL_ERROR", "工具调用 ID 不合法", 502)
                if tool_count + len(ids) > self.settings.agent_max_tool_calls:
                    raise DomainError("TOOL_LIMIT", "工具调用次数达到上限", 409)
                messages.append(
                    {
                        "role": "assistant",
                        "content": reply.content,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {"name": call.name, "arguments": call.arguments},
                            }
                            for call in reply.tool_calls
                        ],
                    }
                )
                for call in reply.tool_calls:
                    await self._execute_tool(call, messages, repeated, trace)
                    tool_count += 1
                continue

            parsed = self._parse_output(reply, trace)
            if parsed is not None:
                return LoopOutcome(
                    parsed=parsed,
                    content=reply.content or "",
                    trace=trace,
                    total_tokens=tokens,
                    usage_estimated=estimated,
                    model_steps=step + 1,
                    tool_calls=tool_count,
                )
            if repairs >= 1:
                raise DomainError("MODEL_INCOMPATIBLE", "结构化输出或证据绑定校验未通过", 502)
            repairs += 1
            messages.append({"role": "assistant", "content": reply.content or ""})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Return only valid JSON matching the specified schema. "
                        "Use exactly the evidence you collected; do not invent identifiers."
                    ),
                }
            )
        raise DomainError("STEP_LIMIT", "模型决策次数达到上限", 409)

    async def _execute_tool(self, call: ToolCall, messages: list[dict], repeated: Counter, trace: list[dict]) -> None:
        self._check_stop()
        try:
            normalized = json.dumps(json.loads(call.arguments), sort_keys=True)
        except (ValueError, TypeError):
            normalized = call.arguments
        fingerprint = (call.name, normalized)
        repeated[fingerprint] += 1
        if repeated[fingerprint] > 2:
            raise DomainError("REPEATED_TOOL_CALL", "模型重复调用同一工具，已停止", 409)
        logical_call_id = uuid4()
        if self.hooks.on_tool_started is not None:
            self.hooks.on_tool_started(call, logical_call_id)
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(self.tools.execute(call.name, call.arguments), 30.0)
        except TimeoutError:
            raise DomainError("TOOL_TIMEOUT", "工具执行超时", 504) from None
        duration_ms = int((time.monotonic() - started) * 1000)
        trace.append(
            {
                "event": "tool.finished",
                "tool": call.name,
                "status": result.status,
                "error_code": result.error.code if result.error else None,
                "evidence_ids": [str(e) for e in result.evidence_ids],
            }
        )
        if self.hooks.on_tool_finished is not None:
            self.hooks.on_tool_finished(call, logical_call_id, result, duration_ms)
        messages.append(
            {"role": "tool", "tool_call_id": call.id, "content": result.model_dump_json()}
        )

    def _parse_output(self, reply, trace: list[dict]) -> BaseModel | None:
        try:
            parsed = self.output_model.model_validate_json(reply.content or "")
        except (ValidationError, ValueError):
            return None
        if self.validate_output is not None:
            try:
                self.validate_output(parsed)
            except (ValueError, DomainError):
                return None
        return parsed


def input_fingerprint(messages: list[dict]) -> str:
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "AgentLoop",
    "LoopHooks",
    "LoopOutcome",
    "LoopPolicy",
    "ToolsPort",
    "input_fingerprint",
]
