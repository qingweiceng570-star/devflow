import asyncio
import json
from uuid import uuid4

import pytest

from app.agents.runtime import AgentRuntime
from app.core.errors import DomainError
from app.integrations.llm.fixture import FixtureModel
from app.integrations.llm.protocol import ModelReply, ToolCall
from app.tools.registry import ToolRegistry


@pytest.mark.parametrize("case_id", ["normal_pr", "risk_pr", "failed_ci"])
async def test_fixture_tool_loop_binds_evidence(settings, scope, case_id):
    result = await AgentRuntime(FixtureModel(), ToolRegistry(scope), settings).run(case_id)
    assert all(result["checks"].values())
    assert result["usage"]["model_steps"] == 2
    assert result["usage"]["tool_calls"] == 1


async def test_unknown_tool_is_rejected(scope):
    result = await ToolRegistry(scope).execute("close_issue", "{}")
    assert result.status == "error"
    assert result.error.code == "UNKNOWN_TOOL"


@pytest.mark.parametrize(
    "arguments",
    ["not JSON", '{"case_id":"../../.env"}', '{"case_id":"normal_pr","repo_id":"other"}'],
)
async def test_invalid_tool_args_are_not_executed(scope, arguments):
    tools = ToolRegistry(scope)
    result = await tools.execute("read_sample", arguments)
    assert result.error.code == "INVALID_TOOL_ARGUMENTS"
    assert not tools.successful_evidence


async def test_repository_scope_is_enforced(scope):
    scope.repo_id = uuid4()
    result = await ToolRegistry(scope).execute("read_sample", '{"case_id":"normal_pr"}')
    assert result.error.code == "FORBIDDEN"


class RepeatingModel:
    async def complete(self, request):
        return ModelReply(
            tool_calls=[
                ToolCall(id=str(uuid4()), name="read_sample", arguments='{"case_id":"normal_pr"}')
            ],
            finish_reason="tool_calls",
        )


async def test_repeated_calls_stop(settings, scope):
    with pytest.raises(DomainError, match="重复") as caught:
        await AgentRuntime(RepeatingModel(), ToolRegistry(scope), settings).run("normal_pr")
    assert caught.value.code == "REPEATED_TOOL_CALL"


async def test_tool_call_limit(settings, scope):
    settings.agent_max_tool_calls = 1
    with pytest.raises(DomainError) as caught:
        await AgentRuntime(RepeatingModel(), ToolRegistry(scope), settings).run("normal_pr")
    assert caught.value.code == "TOOL_LIMIT"


class InvalidAnswerModel(FixtureModel):
    async def complete(self, request):
        reply = await super().complete(request)
        if reply.content:
            answer = json.loads(reply.content)
            answer["evidence_id"] = str(uuid4())
            reply.content = json.dumps(answer)
        return reply


async def test_fabricated_evidence_fails_after_one_repair(settings, scope):
    with pytest.raises(DomainError) as caught:
        await AgentRuntime(InvalidAnswerModel(), ToolRegistry(scope), settings).run("normal_pr")
    assert caught.value.code == "MODEL_INCOMPATIBLE"


async def test_cancel_before_model_call(settings, scope):
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(DomainError) as caught:
        await AgentRuntime(FixtureModel(), ToolRegistry(scope), settings).run("normal_pr", cancel)
    assert caught.value.code == "CANCELLED"


async def test_token_budget_before_call(settings, scope):
    settings.agent_total_token_budget = 1000
    with pytest.raises(DomainError) as caught:
        await AgentRuntime(FixtureModel(), ToolRegistry(scope), settings).run("normal_pr")
    assert caught.value.code == "BUDGET_EXCEEDED"


async def test_timeout(settings, scope):
    class SlowModel:
        async def complete(self, request):
            await asyncio.sleep(2)

    settings.agent_timeout_seconds = 1
    with pytest.raises(DomainError) as caught:
        await AgentRuntime(SlowModel(), ToolRegistry(scope), settings).run("normal_pr")
    assert caught.value.code == "AGENT_TIMEOUT"
