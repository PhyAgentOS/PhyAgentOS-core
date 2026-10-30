"""Token accounting and context-window estimates for the Core CLI.

The ledger is fed from events that are already durable. It never scans the
store per delta and it never invents a number that the provider did not
report:

* a completed model call contributes its usage once, keyed by
  ``model_call_id``; a duplicate completion for the same call only refines
  the stored snapshot, it is never added a second time;
* cache-read and reasoning tokens are informational fields. They are never
  added to the prompt/completion counts, because some SDKs already include
  them in ``total_tokens``;
* a missing usage field stays missing. Only an explicit ``0`` is shown as
  zero;
* a call that failed or was interrupted is expected to have no usage, so it
  does not make the history incomplete. A *completed* call without usage
  does, and the summary says so.

``estimate_context`` answers the one question the status line asks: how large
would the *next* request be? A provider-reported ``prompt_tokens`` value is
used as ``exact`` only while the request it describes has not been extended
by a reply, tool result, or a new turn. Once history grows, the number is an
estimate of the next request, never the old exact value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.utils.helpers import estimate_prompt_tokens_chain

__all__ = [
    "ContextSnapshot",
    "TokenUsage",
    "UsageLedger",
    "UsageTotals",
    "estimate_context",
    "estimate_context_from_conversation",
    "format_context_label",
    "format_session_totals",
    "format_turn_summary",
    "parse_usage",
]

_PROMPT_KEYS = ("prompt_tokens", "input_tokens")
_COMPLETION_KEYS = ("completion_tokens", "output_tokens")
_TOTAL_KEYS = ("total_tokens",)
_CACHE_KEYS = (
    "cached_tokens",
    "cache_read_input_tokens",
    "cache_read_tokens",
    "cached_input_tokens",
)
_REASONING_KEYS = ("reasoning_tokens",)
_NESTED_PROMPT_DETAILS = ("prompt_tokens_details", "input_tokens_details")
_NESTED_COMPLETION_DETAILS = ("completion_tokens_details", "output_tokens_details")


def _int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _first_int(source: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        value = _int_value(source.get(key))
        if value is not None:
            return value
    return None


def _nested_int(source: Mapping[str, Any], parents: Sequence[str], keys: Sequence[str]) -> int | None:
    for parent in parents:
        value = source.get(parent)
        if isinstance(value, Mapping):
            found = _first_int(value, keys)
            if found is not None:
                return found
    return None


def _pick(*values: int | None) -> int | None:
    """Latest non-missing value wins; for cumulative reports use the larger."""
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _merge_fields(left: dict[str, int | None], right: Mapping[str, int | None]) -> dict[str, int | None]:
    merged = dict(left)
    for key, value in right.items():
        merged[key] = _pick(merged.get(key), value)
    return merged


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """One provider usage snapshot, with ``None`` for fields it did not send."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None

    def as_fields(self) -> dict[str, int | None]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }

    @classmethod
    def from_fields(cls, fields: Mapping[str, int | None]) -> "TokenUsage":
        return cls(
            prompt_tokens=fields.get("prompt_tokens"),
            completion_tokens=fields.get("completion_tokens"),
            total_tokens=fields.get("total_tokens"),
            cached_tokens=fields.get("cached_tokens"),
            reasoning_tokens=fields.get("reasoning_tokens"),
        )

    def has_any(self) -> bool:
        return any(value is not None for value in self.as_fields().values())


def parse_usage(raw: object) -> TokenUsage:
    """Read OpenAI/LiteLLM-shaped usage without filling absent fields with 0."""
    if not isinstance(raw, Mapping):
        return TokenUsage()
    prompt = _first_int(raw, _PROMPT_KEYS)
    completion = _first_int(raw, _COMPLETION_KEYS)
    total = _first_int(raw, _TOTAL_KEYS)
    cached = _nested_int(raw, _NESTED_PROMPT_DETAILS, _CACHE_KEYS)
    if cached is None:
        cached = _first_int(raw, _CACHE_KEYS)
    reasoning = _nested_int(raw, _NESTED_COMPLETION_DETAILS, _REASONING_KEYS)
    if reasoning is None:
        reasoning = _first_int(raw, _REASONING_KEYS)
    return TokenUsage(prompt, completion, total, cached, reasoning)


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Aggregated usage for one turn or one session."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_hit_percent: float | None = None
    model_calls: int = 0
    tool_calls: int = 0
    complete: bool = True
    missing_usage_calls: int = 0

    def has_any(self) -> bool:
        return any(
            value is not None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.total_tokens,
                self.cached_tokens,
                self.reasoning_tokens,
            )
        )


@dataclass(slots=True)
class _Call:
    turn_id: str | None
    step_id: str | None = None
    fields: dict[str, int | None] = field(default_factory=dict)
    started: bool = False
    completed: bool = False
    terminal: bool = False
    external: bool = False


@dataclass(slots=True)
class _Session:
    # The Core identity of a model call is (turn_id, step_id, model_call_id);
    # a provider may legally reuse the same model_call_id in another turn or
    # step. External accounting (compaction) lives in its own namespace.
    calls: dict[tuple[str, str, str], _Call] = field(default_factory=dict)
    tools: set[tuple[str, str | None, str]] = field(default_factory=set)
    external_calls: int = 0


def _call_key(event: RuntimeEvent, model_call_id: str) -> tuple[str, str, str]:
    return (event.turn_id or "", event.step_id or "", model_call_id)


class UsageLedger:
    """Incremental, idempotent usage accounting over persisted events.

    One ledger may cover several sessions in a process (the CLI can switch
    sessions without restarting). Totals are kept per session; a turn is a
    subset of its session, so compaction or any other session-level model call
    stays in the cumulative number and is never reset by a turn.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    def _session(self, session_id: str) -> _Session:
        return self._sessions.setdefault(session_id, _Session())

    def seed(self, events: Sequence[RuntimeEvent]) -> None:
        for event in events:
            self.ingest(event)

    def ingest(self, event: RuntimeEvent) -> None:
        session = self._session(event.session_id)
        model_call_id = event.payload.get("model_call_id")
        if event.type is EventType.MODEL_ATTEMPT_STARTED:
            if isinstance(model_call_id, str) and model_call_id:
                call = session.calls.setdefault(
                    _call_key(event, model_call_id), _Call(event.turn_id, event.step_id)
                )
                call.started = True
        elif event.type is EventType.MODEL_ATTEMPT_COMPLETED:
            if isinstance(model_call_id, str) and model_call_id:
                call = session.calls.setdefault(
                    _call_key(event, model_call_id), _Call(event.turn_id, event.step_id)
                )
                call.started = True
                call.completed = True
                call.terminal = True
                usage = parse_usage(event.payload.get("usage"))
                call.fields = _merge_fields(call.fields, usage.as_fields())
        elif event.type in (
            EventType.MODEL_ATTEMPT_FAILED,
            EventType.MODEL_ATTEMPT_INTERRUPTED,
        ):
            if isinstance(model_call_id, str) and model_call_id:
                call = session.calls.setdefault(
                    _call_key(event, model_call_id), _Call(event.turn_id, event.step_id)
                )
                call.started = True
                call.terminal = True
                # A failed stream may already have reported usage. It was
                # really consumed, so keep it; a failure without usage still
                # leaves the total incomplete, because a model request ran.
                usage = parse_usage(event.payload.get("usage"))
                call.fields = _merge_fields(call.fields, usage.as_fields())
        elif _is_stream_usage_event(event):
            if isinstance(model_call_id, str) and model_call_id:
                call = session.calls.setdefault(
                    _call_key(event, model_call_id), _Call(event.turn_id, event.step_id)
                )
                usage = parse_usage(event.payload.get("usage"))
                call.fields = _merge_fields(call.fields, usage.as_fields())
        elif event.type in (
            EventType.TOOL_INTENT,
            EventType.TOOL_DISPATCHED,
            EventType.TOOL_SETTLED,
        ):
            tool_call_id = event.payload.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id:
                session.tools.add((event.session_id, event.turn_id, tool_call_id))

    def record_usage(
        self,
        session_id: str,
        usage: Mapping[str, Any] | TokenUsage,
        *,
        model_call_id: str | None = None,
        turn_id: str | None = None,
        step_id: str | None = None,
        namespace: str = "compaction",
    ) -> None:
        """Add a model call that has no persisted ``model.attempt.*`` events.

        Context compaction owns its own summarization call. Its usage has its
        own namespace, so a provider/model call id can never collide with a
        Core turn's call. The same key refines an existing entry instead of
        adding a second one.
        """
        parsed = usage if isinstance(usage, TokenUsage) else parse_usage(usage)
        session = self._session(session_id)
        if model_call_id:
            key = (turn_id or "", step_id or "", model_call_id)
        else:
            session.external_calls += 1
            key = ("", f"{namespace}:{session.external_calls}", namespace)
        call = session.calls.setdefault(
            key, _Call(turn_id, step_id, external=model_call_id is None)
        )
        call.started = True
        call.completed = True
        call.terminal = True
        call.fields = _merge_fields(call.fields, parsed.as_fields())

    def record_compaction_attempt(
        self,
        session_id: str,
        attempt_id: int,
        status: str,
        usage: Mapping[str, Any] | TokenUsage | None = None,
    ) -> None:
        """Import one persisted compaction attempt by its stable id.

        ``attempt_id`` is the idempotency key: re-importing a session's
        attempts never adds a second cost. A completed attempt's usage is
        counted from its attempt row only; the checkpoint copy is never added
        here. ``started`` attempts are visible in ``model_calls`` but count as
        usage-incomplete until a terminal status arrives.
        """
        parsed = (
            usage
            if isinstance(usage, TokenUsage)
            else parse_usage(usage or {})
        )
        key = ("", f"compaction:{attempt_id}", "compaction")
        call = self._session(session_id).calls.setdefault(
            key, _Call(None, None, external=True)
        )
        call.started = True
        call.terminal = status in {"completed", "failed", "interrupted"}
        call.completed = status == "completed"
        call.fields = _merge_fields(call.fields, parsed.as_fields())

    def _totals(self, session: _Session, *, turn_id: str | None = None) -> UsageTotals:
        calls = [
            call
            for call in session.calls.values()
            if turn_id is None or call.turn_id == turn_id
        ]

        def normalized_total(call: _Call) -> int | None:
            total = call.fields.get("total_tokens")
            if total is not None:
                return total
            prompt = call.fields.get("prompt_tokens")
            completion = call.fields.get("completion_tokens")
            if prompt is not None and completion is not None:
                return prompt + completion
            return None

        input_values: list[int] = []
        output_values: list[int] = []
        total_values: list[int] = []
        cached_values: list[int] = []
        reasoning_values: list[int] = []
        missing = 0
        complete = True
        for call in calls:
            prompt = call.fields.get("prompt_tokens")
            completion = call.fields.get("completion_tokens")
            total = normalized_total(call)
            if not TokenUsage.from_fields(call.fields).has_any():
                missing += 1
                complete = False
            if prompt is None or completion is None or total is None:
                complete = False
            if prompt is not None:
                input_values.append(prompt)
            if completion is not None:
                output_values.append(completion)
            if total is not None:
                total_values.append(total)
            cached = call.fields.get("cached_tokens")
            if cached is not None:
                cached_values.append(cached)
            reasoning = call.fields.get("reasoning_tokens")
            if reasoning is not None:
                reasoning_values.append(reasoning)

        input_tokens = sum(input_values) if input_values else None
        output_tokens = sum(output_values) if output_values else None
        total_tokens = sum(total_values) if total_values else None
        cached = sum(cached_values) if cached_values else None
        reasoning = sum(reasoning_values) if reasoning_values else None

        cache_hit_percent: float | None = None
        if calls:
            prompt_sum = 0
            cached_sum = 0
            known = True
            for call in calls:
                prompt = call.fields.get("prompt_tokens")
                cached_tokens = call.fields.get("cached_tokens")
                if prompt is None or cached_tokens is None:
                    known = False
                    break
                prompt_sum += prompt
                cached_sum += cached_tokens
            if known and prompt_sum > 0:
                cache_hit_percent = cached_sum / prompt_sum * 100.0

        return UsageTotals(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_tokens=cached,
            reasoning_tokens=reasoning,
            cache_hit_percent=cache_hit_percent,
            model_calls=len(calls),
            tool_calls=len(
                {
                    key
                    for key in session.tools
                    if turn_id is None or key[1] == turn_id
                }
            ),
            complete=complete,
            missing_usage_calls=missing,
        )

    def turn(self, session_id: str, turn_id: str) -> UsageTotals:
        return self._totals(self._session(session_id), turn_id=turn_id)

    def session(self, session_id: str) -> UsageTotals:
        return self._totals(self._session(session_id))


def _is_stream_usage_event(event: RuntimeEvent) -> bool:
    """Recognise a usage-carrying stream event without importing a future type.

    A streaming provider may publish usage before the terminal completion.
    Only an event that actually carries both a call id and a usage mapping is
    treated as usage; a plain text delta is ignored.
    """
    if not isinstance(event.payload.get("usage"), Mapping):
        return False
    return "usage" in event.type.value or event.type.value.endswith("delta")


def format_cache_hit(percent: float | None) -> str:
    return "--%" if percent is None else f"{percent:.0f}%"


def format_turn_summary(turn: UsageTotals, duration_seconds: float | None) -> str:
    duration = "--" if duration_seconds is None else f"{max(0.0, duration_seconds):.1f}"
    parts = [
        f"本轮{duration}s",
        f"模型轮次{turn.model_calls}",
        f"工具调用{turn.tool_calls}",
        f"输入{_format_tokens(turn.input_tokens)}",
        f"输出{_format_tokens(turn.output_tokens)}",
        f"缓存命中{format_cache_hit(turn.cache_hit_percent)}",
    ]
    if turn.reasoning_tokens is not None:
        parts.append(f"推理tokens {turn.reasoning_tokens}")
    suffix = "" if turn.complete else "（历史用量不完整）"
    return " · ".join(parts) + suffix


def format_session_totals(total: UsageTotals) -> str:
    suffix = "" if total.complete else "（不完整）"
    return (
        f"会话累计 输入{_format_tokens(total.input_tokens)} · "
        f"输出{_format_tokens(total.output_tokens)} · "
        f"合计{_format_tokens(total.total_tokens)}{suffix}"
    )


def _format_tokens(value: int | None) -> str:
    return "--" if value is None else str(value)


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """Current prompt size, the model capacity, and where both came from.

    ``tokens`` is the estimate or provider-reported prompt size for the next
    request. ``capacity`` is the effective input window the request guard and
    compaction use. ``capacity_source`` says whether that window is the model's
    metadata, the user's explicit cap, or the assumed fallback; the label marks
    the fallback so an assumed number is not read as a model fact.
    """

    tokens: int | None
    capacity: int | None
    exact: bool = False
    capacity_source: str | None = None
    tokens_source: str | None = None

    @property
    def percent(self) -> float | None:
        if self.tokens is None or not self.capacity or self.capacity <= 0:
            return None
        # Never clamp: an over-limit prompt must stay visibly over 100%.
        return self.tokens / self.capacity * 100.0

    def label(self) -> str:
        if self.tokens is None:
            return "context --"
        used = _compact_tokens(self.tokens)
        if not self.capacity or self.capacity <= 0:
            return f"context {used}/--"
        percent = self.percent
        if percent is None:  # pragma: no cover - capacity checked above
            return f"context {used}/--"
        effective = _compact_tokens(self.capacity)
        prefix = "" if self.exact else "≈"
        assumed = " (assumed)" if self.capacity_source == "fallback" else ""
        return f"context {used}/{effective} {prefix}{percent:.0f}%{assumed}"


def _compact_tokens(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        text = f"{value / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"
    text = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
    return f"{text}M"


def format_context_label(snapshot: ContextSnapshot) -> str:
    return snapshot.label()


def _message_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _tool_list(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _assistant_message(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    content = payload.get("content")
    tool_calls = payload.get("tool_calls")
    if not isinstance(content, str) and not tool_calls:
        return None
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content if isinstance(content, str) else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    reasoning = payload.get("reasoning_content")
    if isinstance(reasoning, str):
        message["reasoning_content"] = reasoning
    blocks = payload.get("thinking_blocks")
    if blocks:
        message["thinking_blocks"] = blocks
    return message


def _tool_message(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    tool_call_id = payload.get("tool_call_id")
    text = payload.get("text")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": payload.get("tool_name") or "tool",
        "content": text if isinstance(text, str) else "",
    }


def estimate_context(
    events: Sequence[RuntimeEvent],
    *,
    provider: Any = None,
    model: str | None = None,
    capacity: int | None = None,
    capacity_source: str | None = None,
    instructions: str | None = None,
) -> ContextSnapshot:
    """Estimate the next request from the newest durable snapshot.

    ``exact`` is used only for the provider-reported prompt size of the last
    prepared request while nothing has extended it. A reply, a tool result or
    a new turn all turn the number back into an estimate.
    """
    events = list(events)
    if not events:
        return _estimate_messages(
            ([{"role": "system", "content": instructions}] if instructions else []),
            None,
            provider=provider,
            model=model,
            capacity=capacity,
            capacity_source=capacity_source,
        )

    last_request_index = _last_index(
        events, lambda event: event.type is EventType.REQUEST_PREPARED
    )
    completed_turn_index = _last_index(
        events, lambda event: event.type is EventType.TURN_COMPLETED
    )

    if last_request_index is not None and completed_turn_index is not None:
        request = events[last_request_index]
        completed = events[completed_turn_index]
        if completed.sequence is not None and request.sequence is not None:
            request_is_from_completed_turn = request.turn_id == completed.turn_id
        else:
            request_is_from_completed_turn = request.turn_id == completed.turn_id
        if request_is_from_completed_turn:
            return _estimate_completed_turn(
                events,
                last_request_index,
                completed,
                provider=provider,
                model=model,
                capacity=capacity,
                capacity_source=capacity_source,
            )

    if last_request_index is not None:
        return _estimate_live_request(
            events,
            last_request_index,
            provider=provider,
            model=model,
            capacity=capacity,
            capacity_source=capacity_source,
        )

    return _estimate_messages(
        ([{"role": "system", "content": instructions}] if instructions else []),
        None,
        provider=provider,
        model=model,
        capacity=capacity,
        capacity_source=capacity_source,
    )


def estimate_context_from_conversation(
    conversation: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    *,
    instructions: str | None = None,
    provider: Any = None,
    model: str | None = None,
    capacity: int | None = None,
    capacity_source: str | None = None,
) -> ContextSnapshot:
    """Estimate the next request from a checkpoint-aware conversation."""
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    messages.extend(dict(message) for message in conversation)
    return _estimate_messages(
        messages,
        [dict(tool) for tool in tools] if tools else None,
        provider=provider,
        model=model,
        capacity=capacity,
        capacity_source=capacity_source,
    )


def _last_index(events: Sequence[RuntimeEvent], predicate) -> int | None:
    for index in range(len(events) - 1, -1, -1):
        if predicate(events[index]):
            return index
    return None


def _estimate_completed_turn(
    events: Sequence[RuntimeEvent],
    request_index: int,
    completed: RuntimeEvent,
    *,
    provider: Any,
    model: str | None,
    capacity: int | None,
    capacity_source: str | None,
) -> ContextSnapshot:
    request = events[request_index]
    messages = _message_list(request.payload.get("messages"))
    tools = _tool_list(request.payload.get("tools"))
    reply = completed.payload.get("reply")
    if isinstance(reply, str):
        messages.append({"role": "assistant", "content": reply})
    return _estimate_messages(
        messages,
        tools,
        provider=provider,
        model=model,
        capacity=capacity,
        capacity_source=capacity_source,
    )


def _estimate_live_request(
    events: Sequence[RuntimeEvent],
    request_index: int,
    *,
    provider: Any,
    model: str | None,
    capacity: int | None,
    capacity_source: str | None,
) -> ContextSnapshot:
    request = events[request_index]
    messages = _message_list(request.payload.get("messages"))
    tools = _tool_list(request.payload.get("tools"))
    additions: list[dict[str, Any]] = []
    for event in events[request_index + 1:]:
        if event.type is EventType.REQUEST_PREPARED:
            continue
        if event.type is EventType.MODEL_ATTEMPT_COMPLETED:
            assistant = _assistant_message(event.payload)
            if assistant is not None:
                additions.append(assistant)
        elif event.type is EventType.TOOL_SETTLED:
            tool = _tool_message(event.payload)
            if tool is not None:
                additions.append(tool)

    last = events[-1]
    if (
        last.type is EventType.MODEL_ATTEMPT_COMPLETED
        and last.payload.get("model_call_id") == request.payload.get("model_call_id")
    ):
        usage = parse_usage(last.payload.get("usage"))
        if usage.prompt_tokens is not None:
            return ContextSnapshot(
                tokens=usage.prompt_tokens,
                capacity=capacity,
                exact=True,
                capacity_source=capacity_source,
                tokens_source="provider_usage",
            )

    return _estimate_messages(
        messages + additions,
        tools,
        provider=provider,
        model=model,
        capacity=capacity,
        capacity_source=capacity_source,
    )


def _estimate_messages(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    provider: Any,
    model: str | None,
    capacity: int | None,
    capacity_source: str | None,
) -> ContextSnapshot:
    if not messages and not tools:
        return ContextSnapshot(
            tokens=None, capacity=capacity, exact=False, capacity_source=capacity_source
        )
    tokens, source = estimate_prompt_tokens_chain(provider, model, messages, tools)
    return ContextSnapshot(
        tokens=tokens or None,
        capacity=capacity,
        exact=False,
        capacity_source=capacity_source,
        tokens_source=source if tokens else None,
    )


def usage_from_event(event: RuntimeEvent) -> TokenUsage | None:
    """Convenience for callers that only have one event."""
    if not isinstance(event.payload.get("usage"), Mapping):
        return None
    return parse_usage(event.payload.get("usage"))


def usage_payload_keys() -> tuple[str, ...]:
    """Documented key names accepted by :func:`parse_usage`."""
    return tuple(
        dict.fromkeys(
            (
                *_PROMPT_KEYS,
                *_COMPLETION_KEYS,
                *_TOTAL_KEYS,
                *_CACHE_KEYS,
                *_REASONING_KEYS,
                *_NESTED_PROMPT_DETAILS,
                *_NESTED_COMPLETION_DETAILS,
            )
        )
    )
