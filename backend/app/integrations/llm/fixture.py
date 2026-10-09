import json

from app.integrations.llm.protocol import ModelReply, ModelRequest, ToolCall


class FixtureModel:
    """Deterministic protocol replay; never presented as a live LLM."""

    async def complete(self, request: ModelRequest) -> ModelReply:
        results = [m for m in request.messages if m["role"] == "tool"]
        if not results:
            case_id = request.messages[-1]["content"]
            return ModelReply(
                tool_calls=[
                    ToolCall(
                        id="fixture-call-1",
                        name="read_sample",
                        arguments=json.dumps({"case_id": case_id}),
                    )
                ],
                finish_reason="tool_calls",
                input_tokens=100,
                output_tokens=20,
            )
        result = json.loads(results[-1]["content"])
        return ModelReply(
            content=json.dumps(
                {
                    "status": "ok",
                    "summary": result["data"]["description"],
                    "observed_value": result["data"]["verification_value"],
                    "evidence_id": result["evidence_ids"][0],
                },
                ensure_ascii=False,
            ),
            input_tokens=200,
            output_tokens=60,
        )
