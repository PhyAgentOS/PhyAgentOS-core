"""Provider error codes stay structured, short and separate from error prose.

A context-overflow recovery is only allowed when the provider itself reports a
machine-readable code. These tests use local fakes (fake SDK clients, httpx
MockTransport) so no request leaves the machine.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import litellm
import openai
import pytest

from PhyAgentOS.providers.azure_openai_provider import AzureOpenAIProvider
from PhyAgentOS.providers.base import LLMProvider, LLMResponse, StreamEvent
from PhyAgentOS.providers.custom_provider import CustomProvider
from PhyAgentOS.providers.errors import describe_provider_error, extract_error_code
from PhyAgentOS.providers.litellm_provider import LiteLLMProvider
from PhyAgentOS.providers.openai_responses_provider import OpenAIResponsesProvider

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sdk_error(
    *,
    code: str | None = None,
    message: str = "request rejected",
    status: int = 400,
    error_cls: type = openai.BadRequestError,
) -> Exception:
    """Build a real OpenAI SDK error carrying an optional body ``code``."""
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    error: dict[str, Any] = {"message": message}
    if code is not None:
        error["code"] = code
    body = {"error": error}
    response = httpx.Response(status, request=request, json=body)
    return error_cls(message, response=response, body=body)


class _FakeBodyError(Exception):
    """SDK-shaped exception with an explicit ``body`` but no ``code`` attr."""

    def __init__(self, body: Any) -> None:
        super().__init__("request rejected")
        self.body = body


class _ResponseWithJSON:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeBodylessError(Exception):
    def __init__(self, response: Any) -> None:
        super().__init__("request rejected")
        self.body = None
        self.response = response


class _CompletionsStub:
    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _ClientStub:
    def __init__(self, items: list[Any]) -> None:
        self.completions = _CompletionsStub(items)
        self.chat = SimpleNamespace(completions=self.completions)


class _FakeStream:
    def __init__(self, items: list[Any]) -> None:
        self._items = iter(items)
        self.closed = 0

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> Any:
        try:
            item = next(self._items)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed += 1


def _usage_chunk(prompt_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=2,
            total_tokens=prompt_tokens + 2,
        ),
    )


class _RaisingProvider(LLMProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error
        self.calls = 0
        self._CHAT_RETRY_DELAYS = (0, 0, 0)

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        raise self._error

    def get_default_model(self) -> str:
        return "test-model"


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    ["context_length_exceeded", "context_window_exceeded"],
)
def test_known_overflow_codes_map_to_canonical_name(code: str) -> None:
    assert extract_error_code(_sdk_error(code=code)) == "context_overflow"


def test_unknown_short_code_is_preserved() -> None:
    assert extract_error_code(_sdk_error(code="invalid_api_key")) == "invalid_api_key"


def test_code_is_read_from_a_json_string_body() -> None:
    error = _FakeBodyError('{"error": {"code": "context_length_exceeded"}}')

    assert extract_error_code(error) == "context_overflow"


def test_prose_body_without_code_is_not_structured_evidence() -> None:
    error = _sdk_error(message="too many tokens for this model")

    assert extract_error_code(error) is None


def test_response_json_is_the_last_structured_source() -> None:
    error = _FakeBodylessError(
        _ResponseWithJSON({"error": {"code": "rate_limit_exceeded"}})
    )

    assert extract_error_code(error) == "rate_limit_exceeded"


def test_unparseable_response_json_falls_back_to_none() -> None:
    error = _FakeBodylessError(_ResponseWithJSON(ValueError("not json")))

    assert extract_error_code(error) is None


@pytest.mark.parametrize(
    "code",
    [
        "too many tokens for this model",  # whitespace: prose, not a code
        "sk-LEAKCANARY1234567890",
        "Bearer LEAKCANARY-TOKEN",
        "x" * 80,
        "https://user:pass@api.example.test/v1",
    ],
)
def test_unsafe_codes_are_dropped_instead_of_echoed(code: str) -> None:
    assert extract_error_code(SimpleNamespace(code=code)) is None


def test_litellm_overflow_exception_type_is_structured_evidence() -> None:
    error = litellm.ContextWindowExceededError(
        message="prompt is too long", model="m", llm_provider="openai"
    )

    assert extract_error_code(error) == "context_overflow"


def test_response_fields_default_to_no_code() -> None:
    assert LLMResponse(content="ok").error_code is None
    assert StreamEvent(type="delta", content="x").error_code is None


# ---------------------------------------------------------------------------
# blocking path and retries
# ---------------------------------------------------------------------------


async def test_blocking_error_keeps_code_and_redacted_text() -> None:
    error = _sdk_error(
        code="context_length_exceeded",
        message="maximum context reached for Bearer sk-LEAKCANARY1234567890",
    )
    provider = _RaisingProvider(error)

    response = await provider.chat_with_retry([{"role": "user", "content": "hi"}])

    assert response.finish_reason == "error"
    assert response.error_code == "context_overflow"
    assert response.content == describe_provider_error(error)
    assert "LEAKCANARY" not in (response.content or "")
    assert provider.calls == 1  # a permanent 400 is not retried


async def test_prose_only_failure_is_not_confirmed_as_overflow() -> None:
    provider = _RaisingProvider(RuntimeError("too many tokens in the prompt"))

    response = await provider.chat_with_retry([{"role": "user", "content": "hi"}])

    assert response.finish_reason == "error"
    assert response.error_code is None


async def test_rate_limit_retry_count_is_unchanged_and_code_survives() -> None:
    error = _sdk_error(
        code="rate_limit_exceeded",
        message="rate limit reached",
        status=429,
        error_cls=openai.RateLimitError,
    )
    provider = _RaisingProvider(error)

    response = await provider.chat_with_retry([{"role": "user", "content": "hi"}])

    assert provider.calls == 4  # initial loop + final attempt, unchanged
    assert response.error_code == "rate_limit_exceeded"


# ---------------------------------------------------------------------------
# custom (OpenAI-compatible) provider
# ---------------------------------------------------------------------------


def _custom_provider(items: list[Any]) -> tuple[CustomProvider, _ClientStub]:
    provider = CustomProvider(
        api_key="test-key",
        api_base="http://localhost:9/v1",
        default_model="test-model",
    )
    stub = _ClientStub(items)
    provider._client = stub
    return provider, stub


async def test_custom_blocking_error_preserves_body_code() -> None:
    provider, _ = _custom_provider([_sdk_error(code="context_length_exceeded")])

    response = await provider.chat([{"role": "user", "content": "hi"}])

    assert response.finish_reason == "error"
    assert response.error_code == "context_overflow"


async def test_custom_stream_keeps_code_and_partial_usage() -> None:
    provider, stub = _custom_provider(
        [
            _FakeStream(
                [
                    _usage_chunk(prompt_tokens=7),
                    _sdk_error(code="context_length_exceeded"),
                ]
            )
        ]
    )
    provider._CHAT_RETRY_DELAYS = (0,)

    events = [
        event
        async for event in provider.chat_stream([{"role": "user", "content": "hi"}])
    ]

    assert [event.type for event in events] == ["usage", "error"]
    assert events[-1].finish_reason == "error"
    assert events[-1].error_code == "context_overflow"
    assert events[-1].usage == {
        "prompt_tokens": 7,
        "completion_tokens": 2,
        "total_tokens": 9,
    }
    assert len(stub.completions.calls) == 1  # overflow is not retried


# ---------------------------------------------------------------------------
# LiteLLM provider
# ---------------------------------------------------------------------------


def _litellm_provider() -> LiteLLMProvider:
    provider = LiteLLMProvider(
        api_key="test-key",
        api_base="http://localhost:9/v1",
        default_model="test-model",
    )
    provider._CHAT_RETRY_DELAYS = (0,)
    return provider


def _install_litellm(monkeypatch: pytest.MonkeyPatch, error: Exception) -> list[int]:
    calls: list[int] = []

    async def fake_acompletion(**kwargs: Any) -> Any:
        calls.append(1)
        raise error

    monkeypatch.setattr(
        "PhyAgentOS.providers.litellm_provider.acompletion", fake_acompletion
    )
    return calls


async def test_litellm_blocking_overflow_exception_keeps_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_litellm(
        monkeypatch,
        litellm.ContextWindowExceededError(
            message="prompt is too long", model="m", llm_provider="openai"
        ),
    )

    response = await _litellm_provider().chat([{"role": "user", "content": "hi"}])

    assert response.finish_reason == "error"
    assert response.error_code == "context_overflow"
    assert len(calls) == 1


async def test_litellm_stream_keeps_overflow_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_litellm(
        monkeypatch,
        litellm.ContextWindowExceededError(
            message="prompt is too long", model="m", llm_provider="openai"
        ),
    )

    events = [
        event
        async for event in _litellm_provider().chat_stream(
            [{"role": "user", "content": "hi"}]
        )
    ]

    assert [event.type for event in events] == ["error"]
    assert events[-1].error_code == "context_overflow"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Azure OpenAI provider (httpx MockTransport)
# ---------------------------------------------------------------------------


def _use_azure_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(
        "PhyAgentOS.providers.azure_openai_provider.httpx.AsyncClient", factory
    )


def _azure_provider() -> AzureOpenAIProvider:
    return AzureOpenAIProvider(
        api_key="test-key", api_base="https://example.openai.azure.com"
    )


async def test_azure_non_200_reads_code_from_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_azure_transport(
        monkeypatch,
        lambda request: httpx.Response(
            400, json={"error": {"code": "context_length_exceeded", "message": "long"}}
        ),
    )

    response = await _azure_provider().chat([{"role": "user", "content": "hi"}])

    assert response.finish_reason == "error"
    assert response.error_code == "context_overflow"
    assert "HTTP 400" in (response.content or "")


async def test_azure_non_json_error_body_falls_back_to_no_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_azure_transport(
        monkeypatch, lambda request: httpx.Response(400, text="upstream rejected")
    )

    response = await _azure_provider().chat([{"role": "user", "content": "hi"}])

    assert response.finish_reason == "error"
    assert response.error_code is None
    assert "HTTP 400" in (response.content or "")


# ---------------------------------------------------------------------------
# OpenAI Responses provider
# ---------------------------------------------------------------------------


def _responses_provider() -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider(
        api_key="test-key",
        api_base="http://localhost:9/v1",
        default_model="test-model",
    )


def _failed_response(error: Any) -> SimpleNamespace:
    return SimpleNamespace(
        output=[],
        output_text="",
        status="failed",
        usage=None,
        error=error,
    )


def test_responses_failed_event_carries_code() -> None:
    parsed = _responses_provider()._parse(
        _failed_response(
            SimpleNamespace(code="context_length_exceeded", message="prompt too long")
        )
    )

    assert parsed.finish_reason == "error"
    assert parsed.error_code == "context_overflow"


def test_responses_failed_prose_without_code_is_not_confirmed() -> None:
    parsed = _responses_provider()._parse(
        _failed_response(SimpleNamespace(message="too many tokens"))
    )

    assert parsed.finish_reason == "error"
    assert parsed.error_code is None


def test_responses_cancelled_has_no_code() -> None:
    response = _failed_response(None)
    response.status = "cancelled"

    parsed = _responses_provider()._parse(response)

    assert parsed.finish_reason == "error"
    assert parsed.error_code is None


async def test_responses_exception_path_keeps_code() -> None:
    provider = _responses_provider()
    provider._client = SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **kwargs: _raise(_sdk_error(code="context_length_exceeded"))
        )
    )

    response = await provider.chat([{"role": "user", "content": "hi"}])

    assert response.finish_reason == "error"
    assert response.error_code == "context_overflow"


async def _raise(error: Exception) -> Any:
    raise error
