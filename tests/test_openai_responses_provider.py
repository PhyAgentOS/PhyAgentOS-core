from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from PhyAgentOS.config.schema import Config
from PhyAgentOS.providers.openai_responses_provider import OpenAIResponsesProvider


def _provider() -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider(
        api_key="test-key", api_base="http://localhost:9/v1", default_model="test-model"
    )


def _fake_response(
    *,
    text: str = "",
    calls: list[dict[str, Any]] | None = None,
    reasoning: list[str] | None = None,
    status: str = "completed",
    usage: dict[str, int] | None = None,
) -> SimpleNamespace:
    output: list[SimpleNamespace] = []
    for chunk in reasoning or []:
        output.append(SimpleNamespace(type="reasoning", summary=[SimpleNamespace(text=chunk)]))
    if text:
        output.append(SimpleNamespace(
            type="message", content=[SimpleNamespace(type="output_text", text=text)]
        ))
    for call in calls or []:
        output.append(SimpleNamespace(type="function_call", **call))
    return SimpleNamespace(
        output=output,
        output_text=text,
        status=status,
        usage=SimpleNamespace(**usage) if usage else None,
    )


class _StubClient:
    def __init__(self, response: SimpleNamespace | Exception):
        self._response = response
        self.captured: dict[str, Any] = {}

    @property
    def responses(self) -> "_StubClient":
        return self

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.captured = kwargs
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _TemperatureRejectingStub:
    """Rejects requests carrying temperature, accepts after it is dropped."""

    def __init__(self, response: SimpleNamespace):
        self._response = response
        self.captured: list[dict[str, Any]] = []

    @property
    def responses(self) -> "_TemperatureRejectingStub":
        return self

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.captured.append(kwargs)
        if "temperature" in kwargs:
            raise RuntimeError(
                "Error code: 400 - Unsupported parameter: 'temperature' is not supported with this model"
            )
        return self._response


# ---------------------------------------------------------------------------
# request translation
# ---------------------------------------------------------------------------


def test_to_input_translates_chat_history_with_tool_round_trip() -> None:
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "what is 2+2?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "add", "arguments": "{\"a\": 2, \"b\": 2}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "4"},
        {"role": "assistant", "content": "2+2 = 4"},
    ]

    items = OpenAIResponsesProvider._to_input(messages)

    assert items == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "what is 2+2?"},
        {"type": "function_call", "call_id": "call_1", "name": "add",
         "arguments": "{\"a\": 2, \"b\": 2}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "4"},
        {"role": "assistant", "content": "2+2 = 4"},
    ]


def test_to_input_translates_multimodal_parts() -> None:
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xyz"}},
    ]}]

    items = OpenAIResponsesProvider._to_input(messages)

    assert items == [{"role": "user", "content": [
        {"type": "input_text", "text": "describe"},
        {"type": "input_image", "image_url": "data:image/png;base64,xyz"},
    ]}]


def test_to_tool_flattens_nested_chat_definition() -> None:
    nested = {"type": "function", "function": {
        "name": "ping", "description": "ping", "parameters": {"type": "object", "properties": {}},
    }}
    flat = {"type": "function", "name": "ping", "description": "ping",
            "parameters": {"type": "object", "properties": {}}}

    assert OpenAIResponsesProvider._to_tool(nested) == flat
    assert OpenAIResponsesProvider._to_tool(flat) == flat


def test_to_tool_choice_maps_nested_dict() -> None:
    assert OpenAIResponsesProvider._to_tool_choice(
        {"type": "function", "function": {"name": "ping"}}
    ) == {"type": "function", "name": "ping"}
    assert OpenAIResponsesProvider._to_tool_choice(None) == "auto"
    assert OpenAIResponsesProvider._to_tool_choice("required") == "required"


# ---------------------------------------------------------------------------
# response parsing
# ---------------------------------------------------------------------------


def test_parse_extracts_tool_calls_usage_and_reasoning() -> None:
    response = _fake_response(
        text="done",
        calls=[{"call_id": "call_9", "name": "ping", "arguments": "{\"x\": 1}"}],
        reasoning=["thinking about it"],
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )

    parsed = _provider()._parse(response)

    assert parsed.content == "done"
    assert parsed.finish_reason == "stop"
    assert parsed.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert parsed.reasoning_content == "thinking about it"
    assert [(c.name, c.arguments) for c in parsed.tool_calls] == [("ping", {"x": 1})]


def test_parse_maps_incomplete_status_to_length() -> None:
    parsed = _provider()._parse(_fake_response(status="incomplete"))
    assert parsed.finish_reason == "length"


def test_parse_maps_failed_status_to_error_with_server_detail() -> None:
    response = _fake_response(status="failed")
    response.error = SimpleNamespace(message="content policy violation")

    parsed = _provider()._parse(response)

    assert parsed.finish_reason == "error"
    assert "content policy violation" in parsed.content


def test_parse_maps_cancelled_status_to_error_without_error_object() -> None:
    # A cancelled response may carry no error object at all; the status
    # itself is the only detail available.
    parsed = _provider()._parse(_fake_response(status="cancelled"))

    assert parsed.finish_reason == "error"
    assert "cancelled" in (parsed.content or "")


# ---------------------------------------------------------------------------
# chat() assembly and error path
# ---------------------------------------------------------------------------


async def test_chat_sends_responses_shaped_request() -> None:
    stub = _StubClient(_fake_response(text="pong"))
    provider = _provider()
    provider._client = stub

    await provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {
            "name": "ping", "description": "p", "parameters": {"type": "object", "properties": {}},
        }}],
        max_tokens=128,
        temperature=0.1,
        reasoning_effort="medium",
    )

    assert stub.captured["model"] == "test-model"
    assert stub.captured["input"] == [{"role": "user", "content": "hi"}]
    assert stub.captured["max_output_tokens"] == 128
    assert stub.captured["temperature"] == 0.1
    assert stub.captured["reasoning"] == {"effort": "medium"}
    assert stub.captured["tools"] == [{"type": "function", "name": "ping", "description": "p",
                                       "parameters": {"type": "object", "properties": {}}}]
    assert stub.captured["tool_choice"] == "auto"
    assert "max_tokens" not in stub.captured


async def test_chat_omits_reasoning_for_none() -> None:
    stub = _StubClient(_fake_response(text="pong"))
    provider = _provider()
    provider._client = stub

    await provider.chat(messages=[{"role": "user", "content": "hi"}], reasoning_effort="none")

    assert "reasoning" not in stub.captured
    assert "tools" not in stub.captured


async def test_chat_wraps_errors_as_error_response() -> None:
    stub = _StubClient(RuntimeError("boom"))
    provider = _provider()
    provider._client = stub

    response = await provider.chat(messages=[{"role": "user", "content": "hi"}])

    assert response.finish_reason == "error"
    assert "boom" in (response.content or "")


async def test_chat_drops_temperature_after_server_rejection() -> None:
    stub = _TemperatureRejectingStub(_fake_response(text="pong"))
    provider = _provider()
    provider._client = stub

    first = await provider.chat(
        messages=[{"role": "user", "content": "hi"}], temperature=0.1
    )
    second = await provider.chat(
        messages=[{"role": "user", "content": "again"}], temperature=0.3
    )

    assert first.content == "pong" and second.content == "pong"
    assert "temperature" in stub.captured[0]
    assert "temperature" not in stub.captured[1]
    # the adaptation is remembered: the second chat call never sends it
    assert len(stub.captured) == 3 and "temperature" not in stub.captured[2]


# ---------------------------------------------------------------------------
# shared direct-client configuration
# ---------------------------------------------------------------------------


def test_timeout_and_retry_resolution_honors_zero() -> None:
    from PhyAgentOS.providers._openai_client import resolve_max_retries, resolve_timeout_s

    assert resolve_timeout_s(None) == 180.0
    assert resolve_timeout_s(0) == 0.0  # 0 is a deliberate choice, not "unset"
    assert resolve_max_retries(None) == 2
    assert resolve_max_retries(0) == 0


# ---------------------------------------------------------------------------
# config routing
# ---------------------------------------------------------------------------


def test_forced_provider_routes_to_openai_responses(tmp_path) -> None:
    config = Config()
    config.agents.defaults.provider = "openai_responses"
    config.agents.defaults.model = "gpt-6-astra-phyagentos"
    config.providers.openai_responses.api_key = "k"
    config.providers.openai_responses.api_base = "http://localhost:9/v1"

    assert config.get_provider_name(config.agents.defaults.model) == "openai_responses"
    assert config.get_provider(config.agents.defaults.model).api_base == "http://localhost:9/v1"
