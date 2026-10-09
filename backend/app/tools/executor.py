"""Controlled tool execution for business agents (README 10.3).

The executor receives only validated model arguments; repository scope,
credentials, workspace root and budgets come from the server-built execution
context. Every call is validated against its Pydantic input model, timed,
bounded and recorded by the caller through hooks.
"""

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import DomainError
from app.domain.contracts import Contract, ToolError, ToolMeta, ToolResult

TOOLSET_VERSION = "m2.1"


class EvidenceSink(Protocol):
    def __call__(
        self,
        *,
        source_type: str,
        content: str | bytes,
        locator: dict,
        source_url: str | None = ...,
        observed_at: datetime | None = ...,
        excerpt_limit: int = ...,
    ) -> UUID: ...


@dataclass
class ToolDeps:
    """Server-side capabilities a handler may use; never model-selectable."""

    settings: Settings
    ctx: Any  # ExecutionContext
    repo: Any  # Repository ORM row
    db_factory: Any
    github_read: Any  # GitHubRead adapter (or a factory for live calls)
    workspace: Any  # WorkspaceManager
    evidence: EvidenceSink
    scratch: dict  # per-run mutable state (e.g. prepared diff scope)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[Contract]
    handler: Callable[..., Awaitable[ToolResult]]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
                "strict": True,
            },
        }


def ok_result(
    data: dict,
    *,
    source: str,
    evidence_ids: list[UUID] | None = None,
    freshness: str = "fresh",
    truncated: bool = False,
    next_cursor: str | None = None,
    warnings: list[str] | None = None,
    status: str = "ok",
) -> ToolResult:
    return ToolResult(
        status=status,
        data=data,
        evidence_ids=evidence_ids or [],
        warnings=warnings or [],
        meta=ToolMeta(
            source=source,
            observed_at=datetime.now(UTC),
            freshness=freshness,
            truncated=truncated,
            next_cursor=next_cursor,
        ),
    )


def error_result(code: str, message: str, *, source: str, retryable: bool = False) -> ToolResult:
    return ToolResult(
        status="error",
        error=ToolError(code=code, message=message, retryable=retryable),
        meta=ToolMeta(source=source, observed_at=datetime.now(UTC), freshness="unknown"),
    )


class ToolExecutor:
    """ToolsPort implementation for AgentLoop."""

    def __init__(
        self,
        specs: list[ToolSpec],
        deps: ToolDeps,
        *,
        on_started: Callable[[str, dict], None] | None = None,
        on_finished: Callable[[str, dict, ToolResult, int], None] | None = None,
    ):
        self._specs = {spec.name: spec for spec in specs}
        self.deps = deps
        self.on_started = on_started
        self.on_finished = on_finished
        self._successful_evidence = 0
        self._served_bytes = 0

    @property
    def schemas(self) -> list[dict]:
        return [spec.schema() for spec in self._specs.values()]

    @property
    def has_evidence(self) -> bool:
        return self._successful_evidence > 0

    @property
    def tool_names(self) -> list[str]:
        return list(self._specs)

    async def execute(self, name: str, raw_arguments: str) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return error_result("UNKNOWN_TOOL", "工具未注册", source="executor")
        try:
            arguments = spec.input_model.model_validate_json(raw_arguments)
        except ValidationError:
            return error_result("INVALID_TOOL_ARGUMENTS", "参数不符合工具契约", source="executor")
        if self.on_started is not None:
            self.on_started(name, arguments.model_dump(mode="json"))
        started = time.monotonic()
        try:
            result = await spec.handler(self.deps, arguments)
        except DomainError as error:
            result = error_result(error.code, error.message, source="executor", retryable=error.retryable)
        except Exception:  # noqa: BLE001 - unexpected handler bugs become stable errors
            result = error_result("TOOL_FAILED", "工具执行出现未预期错误", source="executor")
        duration_ms = int((time.monotonic() - started) * 1000)
        if result.status != "error":
            size = compact_json_bytes(result.data)
            if size > self.deps.settings.tool_result_max_bytes:
                result = error_result("TOOL_RESULT_TOO_LARGE", "工具结果超过单次上限", source="executor")
            elif self._served_bytes + size > self.deps.settings.tool_bytes_total_limit:
                result = error_result(
                    "TOOL_BUDGET_EXCEEDED",
                    "本次运行的模型可见工具结果预算已满，请基于已有证据收束结论",
                    source="executor",
                )
            else:
                self._served_bytes += size
        if result.evidence_ids:
            self._successful_evidence += 1
        if self.on_finished is not None:
            self.on_finished(name, arguments.model_dump(mode="json"), result, duration_ms)
        return result


def db_freshness(observed_at: datetime | None, settings: Settings) -> tuple[str, str | None]:
    """Freshness for DB-backed tool data relative to the health window."""
    if observed_at is None:
        return "unknown", None
    aware = observed_at.replace(tzinfo=UTC) if observed_at.tzinfo is None else observed_at
    age = (datetime.now(UTC) - aware).total_seconds()
    return ("fresh" if age <= settings.repo_health_fresh_seconds else "stale"), aware.isoformat()


def compact_json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))
