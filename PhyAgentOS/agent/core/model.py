"""Provider-backed :class:`ModelAdapter` for the agent core.

The adapter is deliberately thin. It turns one immutable
:class:`~PhyAgentOS.agent.core.types.ModelRequest` snapshot into a single
``provider.chat_with_retry`` call and maps the provider answer back to
:class:`~PhyAgentOS.agent.core.types.ModelResponse`. Retries belong to the
injected provider: this class never retries on its own. It also does not save
conversation state, execute tool calls, or interpret ``finish_reason`` beyond
rejecting failures. Provider ``thinking_blocks`` (for example Anthropic extended
thinking) are carried through; :class:`ModelResponse` takes the read-only
snapshot, so the adapter only checks their shape.

Provider objects are injected at construction time, so this module imports no
provider SDK and no concrete provider. Anything that exposes the existing
``chat_with_retry(messages, tools, model, max_tokens, temperature,
reasoning_effort, tool_choice)`` coroutine is accepted.
"""

from __future__ import annotations

import copy
import inspect
import re
from collections.abc import AsyncIterator, Mapping
from enum import Enum
from typing import Any, Protocol

from .types import (
    ModelCallError,
    ModelRequest,
    ModelResponse,
    ModelStreamDelta,
    ToolCall,
)

__all__ = ["ChatProvider", "ModelAdapter"]

_CREDENTIAL_PATTERNS = (
    re.compile(r"\b(?:sk|pk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|password|passwd|secret|access[_ -]?token|refresh[_ -]?token)"
        r"\s*[:=]\s*[^\s,;}]+",
        re.IGNORECASE,
    ),
)
_MAX_DETAIL_LENGTH = 500


def _safe_detail(value: object) -> str:
    """Redact credential-shaped values and keep the provider's error text."""
    text = str(value) or type(value).__name__
    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub("[REDACTED_CREDENTIAL]", text)
    return text[:_MAX_DETAIL_LENGTH]


class ChatProvider(Protocol):
    """The slice of the provider interface this adapter needs.

    Public so a composition root can type the object it injects without
    importing a provider SDK or a concrete provider class.
    """

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any: ...


class ModelAdapter:
    """Core :class:`ModelAdapter` implementation over an injected provider.

    Example::

        adapter = ModelAdapter(provider)  # provider owns retries and defaults
        response = await adapter.complete(request)

    Only non-``None`` generation parameters from the request are forwarded.
    A request that leaves ``temperature``/``max_tokens``/``reasoning_effort``
    unset passes nothing for them, so the injected provider decides its own
    defaults; the adapter never invents or overrides them.

    ``stream`` is available when ``stream=True`` (the default) and the injected
    provider exposes a callable ``chat_stream``. It yields body-text deltas as
    they arrive and ends with exactly one :class:`ModelResponse`; the loop
    driver, not the adapter, persists deltas and history. Providers without
    ``chat_stream`` keep the old ``complete`` path. Passing ``stream=False``
    disables native streaming explicitly.
    """

    def __init__(self, provider: ChatProvider, *, stream: bool = True) -> None:
        self._provider = provider
        self._stream_enabled = stream

    def _generation_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        generation: dict[str, Any] = {}
        if request.model is not None:
            generation["model"] = request.model
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.max_tokens is not None:
            generation["max_tokens"] = request.max_tokens
        if request.reasoning_effort is not None:
            generation["reasoning_effort"] = request.reasoning_effort
        if request.tool_choice is not None:
            generation["tool_choice"] = request.tool_choice
        return generation

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Call the provider once and map its answer, or raise ModelCallError."""
        generation = self._generation_kwargs(request)

        # Both helpers return deep copies; the request snapshot is never mutated.
        messages = request.to_messages()
        tools = request.to_provider_tools()

        try:
            response = await self._provider.chat_with_retry(
                messages=messages,
                tools=tools or None,
                **generation,
            )
        except Exception as exc:  # provider failures are not answers
            raise ModelCallError(
                request.model_call_id,
                _safe_detail(f"{type(exc).__name__}: {exc}"),
            ) from exc

        try:
            return _to_model_response(request.model_call_id, response)
        except ModelCallError:
            raise
        except Exception as exc:  # malformed response shapes are call failures
            raise ModelCallError(
                request.model_call_id,
                _safe_detail(f"malformed provider response: {type(exc).__name__}: {exc}"),
            ) from exc

    def stream(
        self, request: ModelRequest
    ) -> AsyncIterator[ModelStreamDelta | ModelResponse]:
        """Stream one answer as deltas followed by one terminal response.

        The returned object is an async iterator that also exposes the usage
        observed so far (``usage``) and whether a native provider stream was
        used (``native``). Closing it closes the provider stream; the loop does
        this on cancellation and reads ``usage`` afterwards, so token counts
        seen before a stop are not lost. A provider error raises
        :class:`ModelCallError`; if the provider had already reported usage, it
        is attached to the error instead of being dropped.

        When ``stream=False`` or the provider has no ``chat_stream``, the old
        ``complete`` path is replayed and ``native`` is ``False``. That is the
        only fallback: an error from a real stream is never retried through
        ``complete``.
        """
        chat_stream = getattr(self._provider, "chat_stream", None)
        native = (
            self._stream_enabled
            and callable(chat_stream)
            and bool(getattr(self._provider, "supports_native_stream", True))
        )
        usage: dict[str, int] = {}
        return _ModelStream(
            self._stream_response(request, usage), usage, native=native
        )

    async def _stream_response(
        self, request: ModelRequest, usage: dict[str, int]
    ) -> AsyncIterator[ModelStreamDelta | ModelResponse]:
        chat_stream = getattr(self._provider, "chat_stream", None)
        native = (
            self._stream_enabled
            and callable(chat_stream)
            and bool(getattr(self._provider, "supports_native_stream", True))
        )
        if not native:
            yield await self.complete(request)
            return

        generation = self._generation_kwargs(request)
        messages = request.to_messages()
        tools = request.to_provider_tools()
        model_call_id = request.model_call_id

        try:
            events = chat_stream(
                messages=messages,
                tools=tools or None,
                **generation,
            )
        except Exception as exc:
            raise ModelCallError(
                model_call_id,
                _safe_detail(f"{type(exc).__name__}: {exc}"),
            ) from exc
        if not hasattr(events, "__aiter__"):
            raise ModelCallError(
                model_call_id,
                "provider chat_stream did not return an async iterator",
            )

        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        try:
            try:
                async for event in events:
                    # Usage may ride on any provider event, including one just
                    # before a failure. Keep it before deciding how to react.
                    event_usage = _to_usage(getattr(event, "usage", None))
                    if event_usage:
                        usage.update(event_usage)
                    kind = getattr(event, "type", None)
                    if kind == "delta":
                        text = getattr(event, "content", None)
                        if isinstance(text, str) and text:
                            content_parts.append(text)
                            yield ModelStreamDelta(text)
                        continue
                    if kind == "tool_call":
                        tool_calls.append(
                            _to_tool_call(model_call_id, getattr(event, "tool_call", None))
                        )
                        continue
                    if kind == "usage":
                        continue
                    if kind == "done":
                        yield _streamed_response(
                            model_call_id,
                            content="".join(content_parts),
                            tool_calls=tuple(tool_calls),
                            usage=usage,
                            event=event,
                        )
                        return
                    if kind == "error":
                        raise ModelCallError(
                            model_call_id,
                            _safe_detail(
                                getattr(event, "content", None) or "provider stream error"
                            ),
                            usage=usage,
                        )
                    raise ModelCallError(
                        model_call_id,
                        f"provider stream emitted an unknown event type: {kind!r}",
                        usage=usage,
                    )
            except ModelCallError:
                raise
            except Exception as exc:
                raise ModelCallError(
                    model_call_id,
                    _safe_detail(f"{type(exc).__name__}: {exc}"),
                    usage=usage,
                ) from exc
        finally:
            await _close_stream(events)

        raise ModelCallError(
            model_call_id,
            "provider stream ended without a terminal event",
            usage=usage,
        )


class _ModelStream:
    """Async iterator returned by :meth:`ModelAdapter.stream`.

    Exposes the usage observed so far and whether a native provider stream was
    used, so the driver can record a cancelled attempt with what it already
    knows. Closing it closes the inner adapter generator.
    """

    def __init__(
        self,
        events: AsyncIterator[ModelStreamDelta | ModelResponse],
        usage: dict[str, int],
        *,
        native: bool,
    ) -> None:
        self._events = events
        self.usage = usage
        self.native = native

    def __aiter__(self) -> "_ModelStream":
        return self

    async def __anext__(self) -> ModelStreamDelta | ModelResponse:
        return await self._events.__anext__()

    async def aclose(self) -> None:
        await _close_stream(self._events)


def _finish_reason(response: object) -> str:
    raw = getattr(response, "finish_reason", None)
    if isinstance(raw, Enum):
        raw = raw.value
    return raw.strip() if isinstance(raw, str) else ""


def _to_model_response(model_call_id: str, response: object) -> ModelResponse:
    if response is None:
        raise ModelCallError(model_call_id, "provider returned no response")

    finish_reason = _finish_reason(response)
    content = getattr(response, "content", None)
    if finish_reason == "error":
        detail = content if isinstance(content, str) and content.strip() else "provider error"
        raise ModelCallError(model_call_id, _safe_detail(detail))
    if not finish_reason:
        raise ModelCallError(model_call_id, "provider response has no finish_reason")
    if content is not None and not isinstance(content, str):
        raise ModelCallError(model_call_id, "provider response content is not text")

    raw_reasoning = getattr(response, "reasoning_content", None)
    if raw_reasoning is not None and not isinstance(raw_reasoning, str):
        raise ModelCallError(model_call_id, "provider reasoning_content is not text")

    tool_calls = _to_tool_calls(model_call_id, getattr(response, "tool_calls", None))
    if not tool_calls and not (content or "").strip():
        raise ModelCallError(model_call_id, "provider returned no usable answer")

    return ModelResponse(
        content=content,
        finish_reason=finish_reason,
        tool_calls=tool_calls,
        usage=_to_usage(getattr(response, "usage", None)),
        reasoning_content=raw_reasoning,
        thinking_blocks=_to_thinking_blocks(
            model_call_id, getattr(response, "thinking_blocks", None)
        ),
    )


def _streamed_response(
    model_call_id: str,
    *,
    content: str,
    tool_calls: tuple[ToolCall, ...],
    usage: Mapping[str, int],
    event: object,
) -> ModelResponse:
    """Build the terminal response from a provider's ``done`` stream event.

    The provider's done event does not repeat the text, so the adapter passes
    the accumulated deltas. ``finish_reason`` must be terminal and non-error;
    the loop's existing truncation and tool-validation rules then apply to the
    same shape as the non-streaming path.
    """
    finish_reason = _finish_reason(event)
    if not finish_reason:
        raise ModelCallError(model_call_id, "provider stream has no finish_reason")
    if finish_reason == "error":
        detail = getattr(event, "content", None)
        raise ModelCallError(
            model_call_id,
            _safe_detail(detail if isinstance(detail, str) and detail.strip() else "provider error"),
            usage=usage,
        )

    reasoning = getattr(event, "reasoning_content", None)
    if reasoning is not None and not isinstance(reasoning, str):
        raise ModelCallError(model_call_id, "provider reasoning_content is not text")
    if not tool_calls and not content.strip():
        raise ModelCallError(model_call_id, "provider returned no usable answer")

    try:
        return ModelResponse(
            content=content,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            usage=usage,
            reasoning_content=reasoning,
            thinking_blocks=_to_thinking_blocks(
                model_call_id, getattr(event, "thinking_blocks", None)
            ),
        )
    except ModelCallError:
        raise
    except Exception as exc:
        raise ModelCallError(
            model_call_id,
            _safe_detail(f"malformed provider response: {type(exc).__name__}: {exc}"),
        ) from exc


async def _close_stream(stream: object) -> None:
    """Close an async stream/iterator, awaiting its result when needed."""
    for name in ("aclose", "close"):
        close = getattr(stream, name, None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
            return


def _to_usage(raw: object) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    usage: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[str(key)] = value
    return usage


def _to_tool_calls(model_call_id: str, raw_calls: object) -> tuple[ToolCall, ...]:
    if not raw_calls:
        return ()
    if isinstance(raw_calls, (str, bytes, Mapping)):
        raise ModelCallError(model_call_id, "provider tool_calls is not a sequence")
    try:
        iterator = iter(raw_calls)  # type: ignore[call-overload]
    except TypeError as exc:
        raise ModelCallError(model_call_id, "provider tool_calls is not a sequence") from exc
    calls: list[ToolCall] = []
    for raw_call in iterator:
        calls.append(_to_tool_call(model_call_id, raw_call))
    return tuple(calls)


def _to_thinking_blocks(
    model_call_id: str, raw_blocks: object
) -> tuple[Mapping[str, Any], ...] | None:
    """Check the provider shape; :class:`ModelResponse` takes the frozen copy."""
    if raw_blocks is None:
        return None
    if not isinstance(raw_blocks, (list, tuple)):
        raise ModelCallError(model_call_id, "provider thinking_blocks is not a sequence")
    for block in raw_blocks:
        if not isinstance(block, Mapping):
            raise ModelCallError(model_call_id, "provider thinking_blocks must contain mappings")
    return tuple(raw_blocks)


def _to_tool_call(model_call_id: str, raw_call: object) -> ToolCall:
    call_id = getattr(raw_call, "id", None)
    name = getattr(raw_call, "name", None)
    if not isinstance(call_id, str) or not call_id.strip():
        raise ModelCallError(model_call_id, "provider tool call has no id")
    if not isinstance(name, str) or not name.strip():
        raise ModelCallError(model_call_id, "provider tool call has no name")

    arguments = getattr(raw_call, "arguments", None)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ModelCallError(model_call_id, "provider tool call arguments are not an object")

    raw_arguments = getattr(raw_call, "raw_arguments", None)
    return ToolCall(
        tool_call_id=call_id,
        name=name,
        arguments=copy.deepcopy(dict(arguments)),
        raw_arguments=raw_arguments if isinstance(raw_arguments, str) else None,
        provider_fields=_optional_mapping(
            getattr(raw_call, "provider_specific_fields", None)
        ),
        function_provider_fields=_optional_mapping(
            getattr(raw_call, "function_provider_specific_fields", None)
        ),
    )


def _optional_mapping(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return copy.deepcopy(dict(value))
