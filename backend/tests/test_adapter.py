import json

import httpx
import pytest
from pydantic import SecretStr

from app.core.errors import DomainError
from app.integrations.llm.adapter import CompatibleModel
from app.integrations.llm.protocol import ModelRequest


def configure(settings):
    settings.llm_base_url = "https://model.example/v1"
    settings.llm_model = "test-model"
    settings.llm_api_key = SecretStr("test-secret-key")
    return settings


async def test_http_tool_protocol(settings):
    configure(settings)

    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-secret-key"
        assert body["tools"][0]["function"]["name"] == "read_sample"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "read_sample", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    model = CompatibleModel(settings, httpx.MockTransport(handler))
    reply = await model.complete(
        ModelRequest(messages=[], tools=[{"type": "function", "function": {"name": "read_sample"}}])
    )
    assert reply.tool_calls[0].id == "call-1"
    assert reply.input_tokens == 10
    assert not reply.usage_estimated


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "MODEL_AUTH_FAILED"),
        (429, "MODEL_RATE_LIMITED"),
        (400, "MODEL_REQUEST_REJECTED"),
        (500, "MODEL_REQUEST_REJECTED"),
    ],
)
async def test_provider_error_is_redacted(settings, status, code):
    configure(settings)
    model = CompatibleModel(
        settings,
        httpx.MockTransport(lambda _: httpx.Response(status, text="secret upstream response")),
    )
    with pytest.raises(DomainError) as caught:
        await model.complete(ModelRequest(messages=[]))
    assert caught.value.code == code
    assert "secret" not in caught.value.message


async def test_missing_configuration_never_uses_fixture(settings):
    with pytest.raises(DomainError) as caught:
        await CompatibleModel(settings).complete(ModelRequest(messages=[]))
    assert caught.value.code == "CAPABILITY_UNAVAILABLE"


async def test_malformed_provider_response(settings):
    configure(settings)
    model = CompatibleModel(
        settings, httpx.MockTransport(lambda _: httpx.Response(200, json={"wrong": "schema"}))
    )
    with pytest.raises(DomainError) as caught:
        await model.complete(ModelRequest(messages=[]))
    assert caught.value.code == "MODEL_PROTOCOL_ERROR"


async def test_structured_output_request(settings):
    configure(settings)

    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["strict"] is True
        assert "tools" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    model = CompatibleModel(settings, httpx.MockTransport(handler))
    reply = await model.complete(ModelRequest(messages=[], output_schema={"type": "object"}))
    assert reply.usage_estimated


async def test_structured_output_json_object_mode(settings):
    configure(settings)
    settings.llm_structured_output = "json_object"

    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert "json_schema" not in body["response_format"]
        last = body["messages"][-1]
        assert last["role"] == "system"
        assert "JSON schema" in last["content"]
        assert '"type": "object"' in last["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    model = CompatibleModel(settings, httpx.MockTransport(handler))
    messages = [{"role": "user", "content": "hi"}]
    reply = await model.complete(ModelRequest(messages=messages, output_schema={"type": "object"}))
    assert reply.content == "{}"
    assert messages == [{"role": "user", "content": "hi"}]
