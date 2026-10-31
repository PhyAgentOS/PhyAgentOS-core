"""Only structured provider evidence may authorize context-overflow recovery."""

from types import SimpleNamespace

import pytest

from PhyAgentOS.agent.core.model import ModelAdapter
from PhyAgentOS.agent.core.types import ModelCallError, ModelRequest


def request():
    return ModelRequest(model_call_id="overflow-probe", messages=({"role": "user", "content": "hi"},))


@pytest.mark.parametrize("error_code, expected", [
    ("context_length_exceeded", "context_overflow"),
    ("context_overflow", "context_overflow"),
    (None, None),
    (["not-a-code"], None),
])
async def test_response_error_code_preserved_without_parsing_prose(error_code, expected):
    class Provider:
        async def chat_with_retry(self, **kwargs):
            return SimpleNamespace(
                finish_reason="error", content="too many tokens",
                error_code=error_code, usage={"prompt_tokens": 11},
            )

    with pytest.raises(ModelCallError) as caught:
        await ModelAdapter(Provider()).complete(request())
    assert caught.value.code == expected
    assert caught.value.usage["prompt_tokens"] == 11


async def test_structured_sdk_exception_body_code_is_preserved():
    class SDKError(Exception):
        body = {"error": {"code": "context_length_exceeded"}}

    class Provider:
        async def chat_with_retry(self, **kwargs):
            raise SDKError("request rejected")

    with pytest.raises(ModelCallError) as caught:
        await ModelAdapter(Provider()).complete(request())
    assert caught.value.code == "context_overflow"


async def test_stream_error_code_and_partial_usage_are_preserved():
    class Provider:
        supports_native_stream = True

        async def chat_stream(self, **kwargs):
            yield SimpleNamespace(type="usage", usage={"prompt_tokens": 7})
            yield SimpleNamespace(
                type="error", content="request rejected", usage={},
                error_code="context_length_exceeded",
            )

    with pytest.raises(ModelCallError) as caught:
        async for _event in ModelAdapter(Provider()).stream(request()):
            pass
    assert caught.value.code == "context_overflow"
    assert caught.value.usage["prompt_tokens"] == 7
