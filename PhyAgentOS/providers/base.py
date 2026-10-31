"""Base LLM provider interface."""

import asyncio
import inspect
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

import json_repair

from PhyAgentOS.providers.errors import describe_provider_error, extract_error_code

try:
    from loguru import logger
except ImportError:  # pragma: no cover - fallback for lightweight test envs
    logger = logging.getLogger(__name__)


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]
    provider_specific_fields: dict[str, Any] | None = None
    function_provider_specific_fields: dict[str, Any] | None = None
    # Original JSON payload as received. Kept so callers can reject a
    # repaired-but-truncated payload before executing a tool.
    raw_arguments: str | None = None

    def to_openai_tool_call(self) -> dict[str, Any]:
        """Serialize to an OpenAI-style tool_call payload."""
        tool_call = {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": (
                    self.raw_arguments
                    if self.raw_arguments is not None
                    else json.dumps(self.arguments, ensure_ascii=False)
                ),
            },
        }
        if self.provider_specific_fields:
            tool_call["provider_specific_fields"] = self.provider_specific_fields
        if self.function_provider_specific_fields:
            tool_call["function"]["provider_specific_fields"] = self.function_provider_specific_fields
        return tool_call


@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None  # Kimi, DeepSeek-R1 etc.
    thinking_blocks: list[dict] | None = None  # Anthropic extended thinking
    # Provider's own short error code (for example ``context_overflow``).
    # Only structured evidence is kept; never inferred from error prose.
    error_code: str | None = None

    @property
    def has_tool_calls(self) -> bool:
        """Check if response contains tool calls."""
        return len(self.tool_calls) > 0


@dataclass
class StreamEvent:
    """One event from a streaming completion.

    ``type`` is one of ``delta`` (incremental text), ``tool_call`` (a fully
    assembled call), ``usage`` (token accounting), ``done`` (terminal success),
    or ``error`` (terminal failure). Once any event has been produced the
    stream is never retried or replayed.
    """

    type: str
    content: str | None = None
    tool_call: ToolCallRequest | None = None
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None
    thinking_blocks: list[dict] | None = None
    # Short provider error code carried on a terminal ``error`` event.
    error_code: str | None = None


class LLMCallError(RuntimeError):
    """A completion could not be obtained from the provider.

    Providers report transport and API failures as an ``LLMResponse`` with
    ``finish_reason="error"`` instead of raising, because most call sites want
    to inspect the failure and carry on (verification, memory consolidation,
    the experience analyzer). The agent loop is not one of those: a failed call
    is not an answer, and treating it as one ended the turn with the provider's
    error text as the assistant's reply — a reply the session and the caller
    could not tell apart from a real one.

    Raised by the agent loop when a completion comes back with
    ``finish_reason="error"``. Transient failures (timeouts included) reach it
    only after ``chat_with_retry`` has spent its retries; permanent ones — a
    400 the endpoint will reject again — are returned unretried on the first
    attempt, so the error is not necessarily an availability problem.
    ``detail`` carries the provider's own error text; ``messages`` carries the
    partial conversation so the caller can persist the user's message and any
    completed tool exchanges before replying.
    """

    def __init__(self, detail: str, messages: list[dict[str, Any]] | None = None) -> None:
        self.detail = detail
        self.messages: list[dict[str, Any]] = list(messages) if messages is not None else []
        super().__init__(detail)


@dataclass(frozen=True)
class GenerationSettings:
    """Default generation parameters for LLM calls.

    Stored on the provider so every call site inherits the same defaults
    without having to pass temperature / max_tokens / reasoning_effort
    through every layer.  Individual call sites can still override by
    passing explicit keyword arguments to chat() / chat_with_retry().
    """

    temperature: float = 0.7
    max_tokens: int = 4096
    reasoning_effort: str | None = None


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.

    Implementations should handle the specifics of each provider's API
    while maintaining a consistent interface.
    """

    #: True only when this class implements its own ``chat_stream``. The core
    #: adapter uses it to tell a real stream from the inherited non-streaming
    #: fallback, so a provider that only implements ``chat`` keeps the old
    #: single-response path and is reported as a fallback, not as streaming.
    supports_native_stream: bool = False

    _CHAT_RETRY_DELAYS = (1, 2, 4)
    _TRANSIENT_ERROR_MARKERS = (
        "429",
        "rate limit",
        "500",
        "502",
        "503",
        "504",
        "overloaded",
        "timeout",
        "timed out",
        "connection",
        "server error",
        "temporarily unavailable",
    )

    _SENTINEL = object()

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base
        self.generation: GenerationSettings = GenerationSettings()

    @staticmethod
    def _sanitize_empty_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace empty text content that causes provider 400 errors.

        Empty content can appear when MCP tools return nothing. Most providers
        reject empty-string content or empty text blocks in list content.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")

            if isinstance(content, str) and not content:
                clean = dict(msg)
                clean["content"] = None if (msg.get("role") == "assistant" and msg.get("tool_calls")) else "(empty)"
                result.append(clean)
                continue

            if isinstance(content, list):
                filtered = [
                    item for item in content
                    if not (
                        isinstance(item, dict)
                        and item.get("type") in ("text", "input_text", "output_text")
                        and not item.get("text")
                    )
                ]
                if len(filtered) != len(content):
                    clean = dict(msg)
                    if filtered:
                        clean["content"] = filtered
                    elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                        clean["content"] = None
                    else:
                        clean["content"] = "(empty)"
                    result.append(clean)
                    continue

            if isinstance(content, dict):
                clean = dict(msg)
                clean["content"] = [content]
                result.append(clean)
                continue

            result.append(msg)
        return result

    @staticmethod
    def _sanitize_request_messages(
        messages: list[dict[str, Any]],
        allowed_keys: frozenset[str],
    ) -> list[dict[str, Any]]:
        """Keep only provider-safe message keys and normalize assistant content."""
        sanitized = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k in allowed_keys}
            if clean.get("role") == "assistant" and "content" not in clean:
                clean["content"] = None
            sanitized.append(clean)
        return sanitized

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions.
            model: Model identifier (provider-specific).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            tool_choice: Tool selection strategy ("auto", "required", or specific tool dict).

        Returns:
            LLMResponse with content and/or tool calls.
        """
        pass

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = _SENTINEL,
        temperature: object = _SENTINEL,
        reasoning_effort: object = _SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Default streaming fallback for providers without native streaming.

        Calls ``chat_with_retry`` and replays the complete response as events,
        so providers that only implement ``chat`` (and test doubles that only
        implement ``chat_with_retry``) keep the same retry and generation
        defaults as the non-streaming path. Providers that support native
        streaming override this method.
        """
        max_tokens, temperature, reasoning_effort = self._resolve_generation(
            max_tokens, temperature, reasoning_effort
        )

        response = await self.chat_with_retry(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )
        if response.finish_reason == "error":
            yield StreamEvent(
                type="error",
                content=response.content,
                finish_reason="error",
                usage=dict(response.usage),
                error_code=response.error_code,
            )
            return
        if not response.finish_reason:
            yield StreamEvent(
                type="error",
                content="stream ended without finish_reason",
                finish_reason="error",
            )
            return
        if response.content is not None:
            yield StreamEvent(type="delta", content=response.content)
        for tool_call in response.tool_calls:
            yield StreamEvent(type="tool_call", tool_call=tool_call)
        yield StreamEvent(
            type="done",
            finish_reason=response.finish_reason,
            usage=dict(response.usage),
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
        )

    async def _chat_stream_with_retry(
        self,
        factory: Callable[[], AsyncIterator[StreamEvent]],
    ) -> AsyncIterator[StreamEvent]:
        """Run a stream factory with retries until the first event is emitted.

        A failure before any event can be retried on transient errors; a
        failure after a delta (or any other event) is reported as a terminal
        ``error`` event and never replayed.
        """
        delays = self._CHAT_RETRY_DELAYS
        last_error: StreamEvent | None = None
        observed_usage: dict[str, int] = {}

        for attempt in range(len(delays) + 1):
            emitted = False
            completed = False
            try:
                async with aclosing(factory()) as stream:
                    async for event in stream:
                        event_usage = getattr(event, "usage", None)
                        if event_usage:
                            # Usage can arrive on its own event before any
                            # text. Remember it so an error after that point
                            # still carries what was already measured.
                            observed_usage.update(event_usage)
                        if event.type == "error" and not emitted:
                            if self._is_transient_error(event.content):
                                last_error = event
                                break
                            yield event
                            return
                        emitted = True
                        yield event
                    completed = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                event = StreamEvent(
                    type="error",
                    content=describe_provider_error(exc),
                    finish_reason="error",
                    usage=dict(observed_usage),
                    error_code=extract_error_code(exc),
                )
                if emitted:
                    yield event
                    return
                last_error = event
                if not self._is_transient_error(event.content):
                    yield event
                    return

            if completed:
                return

            if attempt < len(delays):
                logger.warning(
                    "LLM stream failed before first token (attempt {}/{}), retrying in {}s: {}",
                    attempt + 1,
                    len(delays) + 1,
                    delays[attempt],
                    (last_error.content if last_error else "")[:120],
                )
                await asyncio.sleep(delays[attempt])

        if last_error is not None:
            yield last_error

    def _resolve_generation(
        self,
        max_tokens: object,
        temperature: object,
        reasoning_effort: object,
    ) -> tuple[int, float, str | None]:
        """Apply the provider's generation defaults to sentinel arguments."""
        if max_tokens is self._SENTINEL:
            max_tokens = self.generation.max_tokens
        if temperature is self._SENTINEL:
            temperature = self.generation.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = self.generation.reasoning_effort
        return int(max_tokens), float(temperature), reasoning_effort

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        """Close an async stream iterator, awaiting its result when needed."""
        for name in ("aclose", "close"):
            close = getattr(stream, name, None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
                return

    @staticmethod
    def _is_complete_json_object(raw: str) -> bool:
        """Return True when ``raw`` is a structurally complete JSON object.

        This only checks brace/bracket balance and string termination; the
        actual parsing still goes through ``json_repair`` so streaming matches
        the non-streaming path's tolerance for minor damage.
        """
        text = raw.strip()
        if not text.startswith("{"):
            return False
        depth = 0
        in_string = False
        escaped = False
        for position, char in enumerate(text):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth < 0:
                    return False
                if depth == 0 and position != len(text) - 1:
                    return False
        return depth == 0 and not in_string

    @staticmethod
    def _accumulate_reasoning(
        reasoning_parts: list[str],
        thinking_blocks: list[dict],
        delta: Any,
    ) -> None:
        """Append reasoning deltas to the running transcript."""
        reasoning = getattr(delta, "reasoning_content", None)
        if isinstance(reasoning, str) and reasoning:
            reasoning_parts.append(reasoning)
        blocks = getattr(delta, "thinking_blocks", None)
        if isinstance(blocks, dict):
            thinking_blocks.append(blocks)
        elif blocks:
            thinking_blocks.extend(blocks)

    @staticmethod
    def _usage_to_dict(usage: Any) -> dict[str, int]:
        """Normalize provider usage without inventing token counts.

        Canonical keys are the OpenAI ones (``prompt_tokens``,
        ``completion_tokens``, ``total_tokens``), with the Responses-shaped
        ``input_tokens`` / ``output_tokens`` accepted as aliases. Two optional
        breakdown keys are preserved when the provider reports them:

        * ``cached_tokens`` from ``prompt_tokens_details.cached_tokens``,
          ``input_tokens_details.cached_tokens``, or the provider-native
          DeepSeek / Anthropic cache fields;
        * ``reasoning_tokens`` from ``completion_tokens_details`` /
          ``output_tokens_details``.

        Missing data stays missing: a key that was not reported is absent, not
        ``0``. Cached input is already part of ``prompt_tokens`` under the
        OpenAI (and LiteLLM-normalized Anthropic/DeepSeek) contract, and
        reasoning tokens are already part of ``completion_tokens`` /
        ``total_tokens``, so nothing is added or subtracted here.
        """
        if usage is None:
            return {}

        def get(key: str) -> Any:
            if isinstance(usage, Mapping):
                return usage.get(key)
            return getattr(usage, key, None)

        def as_int(value: Any) -> int | None:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
            return None

        def first(*values: Any) -> int | None:
            for value in values:
                number = as_int(value)
                if number is not None:
                    return number
            return None

        def detail_value(details: Any, key: str) -> Any:
            if details is None:
                return None
            if isinstance(details, Mapping):
                return details.get(key)
            return getattr(details, key, None)

        prompt_details = get("prompt_tokens_details")
        if prompt_details is None:
            prompt_details = get("input_tokens_details")
        completion_details = get("completion_tokens_details")
        if completion_details is None:
            completion_details = get("output_tokens_details")

        normalized = {
            "prompt_tokens": first(get("prompt_tokens"), get("input_tokens")),
            "completion_tokens": first(
                get("completion_tokens"), get("output_tokens")
            ),
            "total_tokens": first(get("total_tokens")),
            "cached_tokens": first(
                detail_value(prompt_details, "cached_tokens"),
                get("cached_tokens"),
                get("prompt_cache_hit_tokens"),
                get("cache_read_input_tokens"),
            ),
            "reasoning_tokens": first(
                detail_value(completion_details, "reasoning_tokens"),
                get("reasoning_tokens"),
            ),
        }
        return {
            key: value for key, value in normalized.items() if value is not None
        }

    @staticmethod
    def _accumulate_tool_fragment(
        pending: dict[int, dict[str, Any]],
        fragment: Any,
    ) -> None:
        """Append an OpenAI-style tool_call delta fragment to its accumulator."""
        index = getattr(fragment, "index", None)
        fragment_id = getattr(fragment, "id", None)
        if index is None:
            if fragment_id:
                # Some gateways omit index on follow-up fragments. Prefer the
                # call with the same id over guessing "the last one".
                matches = [
                    key for key, entry in pending.items()
                    if entry.get("id") == fragment_id
                ]
                index = matches[0] if matches else len(pending)
            else:
                index = max(pending) if pending else 0
        entry = pending.setdefault(index, {"id": None, "name": "", "arguments": ""})
        if fragment_id:
            entry["id"] = fragment_id
        fragment_fields = getattr(fragment, "provider_specific_fields", None)
        if isinstance(fragment_fields, dict):
            entry.setdefault("provider_specific_fields", {}).update(fragment_fields)
        function = getattr(fragment, "function", None)
        if function is not None:
            name = getattr(function, "name", None)
            if name:
                entry["name"] += name
            arguments = getattr(function, "arguments", None)
            if arguments:
                entry["arguments"] += arguments
            function_fields = getattr(function, "provider_specific_fields", None)
            if isinstance(function_fields, dict):
                entry.setdefault("function_provider_specific_fields", {}).update(function_fields)

    @staticmethod
    def _finalize_tool_calls(
        pending: dict[int, dict[str, Any]],
        id_factory: Callable[[int], str],
    ) -> list[ToolCallRequest] | str:
        """Assemble pending fragments into complete tool calls.

        Returns an error string instead of calls when an argument payload is
        truncated or is not a JSON object.
        """
        tool_calls: list[ToolCallRequest] = []
        for index, entry in sorted(pending.items()):
            raw = entry.get("arguments") or "{}"
            name = entry.get("name") or "tool"
            if isinstance(raw, str):
                complete = LLMProvider._is_complete_json_object(raw)
                try:
                    arguments = json_repair.loads(raw)
                except Exception:
                    return (
                        f"Incomplete tool call arguments for {name}; "
                        "the stream ended before the JSON payload was complete."
                    )
                if not isinstance(arguments, dict):
                    return f"Tool call arguments for {name} are not a JSON object."
                if not complete:
                    return (
                        f"Incomplete tool call arguments for {name}; "
                        "the stream ended before the JSON payload was complete."
                    )
            else:
                arguments = raw
                if not isinstance(arguments, dict):
                    return f"Tool call arguments for {name} are not a JSON object."

            tool_calls.append(ToolCallRequest(
                id=entry.get("id") or id_factory(index),
                name=name,
                arguments=arguments,
                provider_specific_fields=entry.get("provider_specific_fields"),
                function_provider_specific_fields=entry.get("function_provider_specific_fields"),
                raw_arguments=raw if isinstance(raw, str) else None,
            ))
        return tool_calls

    async def _iter_openai_stream(
        self,
        stream: Any,
        *,
        id_factory: Callable[[int], str],
    ) -> AsyncIterator[StreamEvent]:
        """Consume one OpenAI-compatible stream and assemble tool calls first."""
        pending: dict[int, dict[str, Any]] = {}
        usage: dict[str, int] = {}
        finish_reason: str | None = None
        reasoning_parts: list[str] = []
        thinking_blocks: list[dict] = []

        try:
            async for chunk in stream:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    normalized = self._usage_to_dict(chunk_usage)
                    if normalized:
                        # Publish usage as soon as the SDK reports it. A caller
                        # may be cancelled before the final chunk; without this
                        # event the already-billed tokens would be invisible.
                        usage.update(normalized)
                        yield StreamEvent(type="usage", usage=dict(normalized))

                for choice in getattr(chunk, "choices", None) or []:
                    delta = getattr(choice, "delta", None)
                    if delta is not None:
                        content = getattr(delta, "content", None)
                        if isinstance(content, str) and content:
                            yield StreamEvent(type="delta", content=content)
                        self._accumulate_reasoning(
                            reasoning_parts, thinking_blocks, delta
                        )
                        for fragment in getattr(delta, "tool_calls", None) or []:
                            self._accumulate_tool_fragment(pending, fragment)
                    reason = getattr(choice, "finish_reason", None)
                    if reason:
                        finish_reason = reason
        finally:
            await self._close_stream(stream)

        if finish_reason is None:
            yield StreamEvent(
                type="error",
                content="stream ended without finish_reason",
                finish_reason="error",
                usage=dict(usage),
            )
            return

        tool_calls = self._finalize_tool_calls(pending, id_factory)
        if isinstance(tool_calls, str):
            yield StreamEvent(
                type="error",
                content=tool_calls,
                finish_reason="error",
                usage=dict(usage),
            )
            return
        for tool_call in tool_calls:
            yield StreamEvent(type="tool_call", tool_call=tool_call)
        yield StreamEvent(
            type="done",
            finish_reason=finish_reason,
            usage=dict(usage),
            reasoning_content="".join(reasoning_parts) or None,
            thinking_blocks=thinking_blocks or None,
        )

    @classmethod
    def _is_transient_error(cls, content: str | None) -> bool:
        err = (content or "").lower()
        return any(marker in err for marker in cls._TRANSIENT_ERROR_MARKERS)

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = _SENTINEL,
        temperature: object = _SENTINEL,
        reasoning_effort: object = _SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
        mode: str | None = None,
    ) -> LLMResponse:
        """Call chat() with retry on transient provider failures.

        Parameters default to ``self.generation`` when not explicitly passed,
        so callers no longer need to thread temperature / max_tokens /
        reasoning_effort through every layer.

        ``mode`` is accepted and ignored: single-model deployments serve
        every mode (main / multimodal / ...) with the same model. Only
        ProvidersManager routes it to a mode-specific provider.
        """
        max_tokens, temperature, reasoning_effort = self._resolve_generation(
            max_tokens, temperature, reasoning_effort
        )

        for attempt, delay in enumerate(self._CHAT_RETRY_DELAYS, start=1):
            try:
                response = await self.chat(
                    messages=messages,
                    tools=tools,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    tool_choice=tool_choice,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                response = LLMResponse(
                    content=describe_provider_error(exc),
                    finish_reason="error",
                    error_code=extract_error_code(exc),
                )

            if response.finish_reason != "error":
                return response
            if not self._is_transient_error(response.content):
                return response

            err = (response.content or "").lower()
            logger.warning(
                "LLM transient error (attempt {}/{}), retrying in {}s: {}",
                attempt,
                len(self._CHAT_RETRY_DELAYS),
                delay,
                err[:120],
            )
            await asyncio.sleep(delay)

        try:
            return await self.chat(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return LLMResponse(
                content=describe_provider_error(exc),
                finish_reason="error",
                error_code=extract_error_code(exc),
            )

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        pass

    def get_context_window(self, model: str | None = None) -> int | None:
        """Return the model's input window when the provider knows it.

        This is a local metadata lookup, never an API request. Return a value
        only when it is exact for this provider and model: a gateway, a local
        deployment, a custom endpoint, or a deployment alias has no trustworthy
        window, so return ``None`` and let the caller apply its configured cap
        or documented fallback. Callers treat ``None`` as "unknown", not as a
        reason to probe the endpoint.
        """
        return None
