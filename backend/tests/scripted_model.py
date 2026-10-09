"""Deterministic scripted model for replaying agent loops without a provider."""

import json
from collections.abc import Callable
from uuid import uuid4

from app.integrations.llm.protocol import ModelReply, ToolCall


class ScriptedModel:
    """Replays a fixed list of steps.

    Each step is one of:
      {"tool": name, "args": {...}}          -> a tool call
      {"content": {...}}                     -> a final structured answer
      {"content_with_evidence": {...}}       -> answer built from the latest tool
                                                result's evidence_ids

    `on_request(index)` fires before each completion so tests can inject side
    effects (e.g. requesting cancellation between steps). Requests are
    recorded; running out of steps raises AssertionError so a looping agent
    fails loudly instead of hanging.
    """

    def __init__(
        self,
        steps: list[dict],
        on_request: Callable[[int], None] | None = None,
    ):
        self.steps = list(steps)
        self.on_request = on_request
        self.requests: list = []
        self.calls = 0

    def _latest_tool_evidence_ids(self, request) -> list[str]:
        for message in reversed(request.messages):
            if message.get("role") == "tool":
                payload = json.loads(message["content"])
                return [str(e) for e in payload.get("evidence_ids") or []]
        return []

    async def complete(self, request) -> ModelReply:
        if self.on_request is not None:
            self.on_request(self.calls)
        self.requests.append(request)
        assert self.calls < len(self.steps), "ScriptedModel ran out of steps"
        step = self.steps[self.calls]
        self.calls += 1
        if "tool" in step:
            return ModelReply(
                tool_calls=[
                    ToolCall(
                        id=f"call-{uuid4().hex[:8]}",
                        name=step["tool"],
                        arguments=json.dumps(step.get("args") or {}),
                    )
                ],
                finish_reason="tool_calls",
                input_tokens=100,
                output_tokens=30,
            )
        if "content_with_evidence" in step:
            spec = step["content_with_evidence"]
            answer = pr_review_answer(verdict=spec.get("verdict", "insufficient_evidence"))
            if step.get("no_verdict"):
                answer.pop("verdict")  # e.g. KnowledgeDraft has no verdict field
            for key, value in spec.items():
                if key not in {"severity", "claim", "location", "verdict"}:
                    answer[key] = value
            evidence_ids = self._latest_tool_evidence_ids(request)
            if evidence_ids:
                answer["findings"] = [
                    {
                        "severity": spec.get("severity", "low"),
                        "claim": spec.get("claim", "示例发现"),
                        "evidence_ids": evidence_ids[:1],
                        "location": spec.get("location", "src/app.py:2"),
                        "suggestion": "请检查该变更",
                    }
                ]
            content = answer
        else:
            content = step["content"]
        return ModelReply(
            content=json.dumps(content, ensure_ascii=False),
            finish_reason="stop",
            input_tokens=200,
            output_tokens=80,
        )


def pr_review_answer(verdict: str = "insufficient_evidence", evidence_ids=None, **extra) -> dict:
    answer = {
        "summary": "测试用审查结论",
        "verdict": verdict,
        "findings": [],
        "hypotheses": [],
        "missing_evidence": [],
        "next_actions": [],
        "limitations": [],
    }
    if evidence_ids:
        answer["findings"] = [
            {
                "severity": extra.get("severity", "low"),
                "claim": extra.get("claim", "示例发现"),
                "evidence_ids": evidence_ids,
                "location": extra.get("location", "src/app.py:2"),
                "suggestion": "请检查该变更",
            }
        ]
    answer.update(
        {k: v for k, v in extra.items() if k not in {"severity", "claim", "location"}}
    )
    return answer
