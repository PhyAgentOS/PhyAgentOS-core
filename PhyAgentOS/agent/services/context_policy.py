"""Automatic context preparation for one turn.

The policy runs before a model request and has two bounded stages:

1. deterministic shortening of old oversized *tool text*; no model call. Tool
   calls and results stay paired because only the result body changes. Status,
   error, domain references and short safety text are copied into the compact
   reference, and the original text stays in the append-only session log.
   Multimodal content and the most recent necessary visual are never replaced
   with an invented attachment path.
2. one summary call over the safe completed prefix, reusing
   :func:`~PhyAgentOS.agent.services.context_compaction.compact_session`. The
   current user task and the recent complete tool pair stay verbatim. The
   summary is recorded as a compaction attempt, but this policy never commits a
   checkpoint: during an unfinished turn the store rightly rejects one. The
   turn owner persists the returned messages in its own event.

Nothing here writes ``session_events``. ``prepare`` returns a reduction only
when the estimated next request actually shrank, and it never loops: one trim
pass plus at most one summary call. ``force=True`` skips the pressure check for
one real context-overflow recovery but keeps the same bounded behavior.

The estimator measures the real next request, including system text and tool
schema. By default it uses the provider token counter through
``estimate_prompt_tokens_chain`` and falls back to a character estimate only
when no counter is available.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PhyAgentOS.utils.helpers import estimate_prompt_tokens_chain

from ..core.model import ChatProvider
from ..core.prompt import is_runtime_context_message
from ..core.session import (
    CHECKPOINT_SUMMARY_NOTICE,
    CheckpointConflictError,
    SQLiteSessionStore,
)
from ..core.types import ExecutionContext, ToolDefinition
from .context_compaction import CompactionError, compact_session

__all__ = ["ContextPolicy", "ContextReduction"]

Estimator = Callable[[Sequence[Mapping[str, Any]], Sequence[ToolDefinition]], int]

_REF_KEYS = ("task_id", "revision_id", "invocation_id", "attempt_id")
_STATE_KEYS = (
    "status", "state", "phase", "action_status", "session_status",
    "cancel_status", "cancellation_status", "domain_status",
)
_SAFETY_HINTS = ("safety", "warning", "notice", "blocker", "guard", "hint")
_VISUAL_HINTS = ("image", "screenshot", "frame", "visual", "video", "camera", "depth")
_EMPTY = (None, False, "", 0, [], {})
_MIN_REFERENCE_SAVING = 128
_MAX_KEPT_TEXT = 300


@dataclass(frozen=True, slots=True)
class ContextReduction:
    """A smaller next request produced without rewriting durable history.

    ``messages`` replaces the conversation history for this request (the
    caller still adds its own system prompt). ``before_tokens`` and
    ``after_tokens`` are measured by the same estimator over the real request,
    including system text and tool schema. ``reason`` says which stage reduced
    the request; a summary failure is never reported as a summary success.
    """

    messages: tuple[Mapping[str, Any], ...]
    before_tokens: int
    after_tokens: int
    reason: str

    def __post_init__(self) -> None:
        messages = tuple(self.messages)
        if not messages:
            raise ValueError("messages must not be empty")
        for message in messages:
            if not isinstance(message, Mapping):
                raise TypeError("messages must contain mappings")
        object.__setattr__(self, "messages", messages)
        for name in ("before_tokens", "after_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        if self.after_tokens >= self.before_tokens:
            raise ValueError("a reduction must make the estimated request smaller")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")


class ContextPolicy:
    """Decide whether one turn needs less context, and prepare replacement messages.

    ``threshold`` is the pressure point. A float in ``(0, 1]`` is a fraction of
    ``capacity`` (``0.8`` means "act above 80% of the window"); an int is an
    absolute token count. The hard ceiling is ``capacity - reserve_tokens``, so
    the pressure point is the smaller of the two. ``capacity`` should be the
    effective window the runtime uses, and ``reserve_tokens`` the reply budget.

    ``estimator`` receives the complete next-request messages (system included)
    and the offered tool definitions. Passing ``None`` uses the provider's
    counter via ``estimate_prompt_tokens_chain`` with the provider argument
    supplied to this policy; select the real provider there, not a UI wrapper,
    so its counter is used first.

    ``system_instructions`` must be the same fixed text the assembler puts in
    the system message. Live prompt fragments are not part of the cache-stable
    system. ``request_overhead`` is an optional callable that returns the extra
    tokens those live fragments add to the next request; it receives the step
    context, so ``prepare`` can detect pressure that only exists after the
    assembler appends the runtime tail. The same overhead is added to before and
    after estimates because one reduction does not shrink live state.
    """

    def __init__(
        self,
        store: SQLiteSessionStore,
        provider: ChatProvider,
        *,
        model: str,
        capacity: int,
        reserve_tokens: int,
        threshold: float | int = 0.8,
        system_instructions: str = "",
        estimator: Estimator | None = None,
        request_overhead: Callable[[ExecutionContext], int] | None = None,
        provider_name: str | None = None,
        effort: str | None = None,
        max_tokens: int | None = None,
        keep_user_turns: int = 1,
        max_retained_chars: int = 40_000,
        max_summary_input_chars: int = 60_000,
        max_message_chars: int = 4_000,
        trim_min_chars: int = 2_000,
    ) -> None:
        if not isinstance(store, SQLiteSessionStore):
            raise TypeError("store must be a SQLiteSessionStore")
        if not callable(getattr(provider, "chat_with_retry", None)):
            raise TypeError("provider must expose chat_with_retry")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValueError("capacity must be a positive int")
        if not isinstance(reserve_tokens, int) or isinstance(reserve_tokens, bool):
            raise ValueError("reserve_tokens must be a non-negative int")
        if reserve_tokens < 0 or reserve_tokens >= capacity:
            raise ValueError("reserve_tokens must be >= 0 and smaller than capacity")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise TypeError("threshold must be an int token count or a float ratio")
        if isinstance(threshold, float) and not 0 < threshold <= 1:
            raise ValueError("a float threshold must be in (0, 1]")
        if isinstance(threshold, int) and threshold < 1:
            raise ValueError("an int threshold must be positive")
        if not isinstance(system_instructions, str):
            raise TypeError("system_instructions must be a string")
        if estimator is not None and not callable(estimator):
            raise TypeError("estimator must be callable or None")
        if request_overhead is not None and not callable(request_overhead):
            raise TypeError("request_overhead must be callable or None")
        if effort is not None and (not isinstance(effort, str) or not effort.strip()):
            raise ValueError("effort must be a non-empty string or None")
        if max_tokens is not None and (
            not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1
        ):
            raise ValueError("max_tokens must be a positive int or None")
        if keep_user_turns < 1:
            raise ValueError("keep_user_turns must be >= 1")
        for name, value in (
            ("max_retained_chars", max_retained_chars),
            ("max_summary_input_chars", max_summary_input_chars),
            ("max_message_chars", max_message_chars),
            ("trim_min_chars", trim_min_chars),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive int")

        self._store = store
        self._provider = provider
        self._model = model
        self._capacity = capacity
        self._reserve_tokens = reserve_tokens
        self._threshold = threshold
        self._system_instructions = system_instructions
        self._estimator = estimator
        self._request_overhead = request_overhead
        self._provider_name = provider_name
        self._effort = effort
        self._max_tokens = max_tokens
        self._keep_user_turns = keep_user_turns
        self._max_retained_chars = max_retained_chars
        self._max_summary_input_chars = max_summary_input_chars
        self._max_message_chars = max_message_chars
        self._trim_min_chars = trim_min_chars
        self._usage_records: list[dict[str, int]] = []

        trigger = (
            threshold * capacity if isinstance(threshold, float) else threshold
        )
        self._limit = max(1, min(int(trigger), capacity - reserve_tokens))

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def limit_tokens(self) -> int:
        """Estimated request size above which ``prepare`` acts."""
        return self._limit

    def estimated_tokens(
        self,
        history: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolDefinition] = (),
        *,
        context: ExecutionContext | None = None,
    ) -> int:
        """Measure the next request with the configured estimator.

        Pass ``context`` to include ``request_overhead`` for live fragments.
        """
        total = self._request_estimate(history, tools)
        if context is not None:
            total += self._overhead(context)
        return total

    def drain_usage(self) -> tuple[Mapping[str, int], ...]:
        """Return and clear usage from summary calls made since the last drain.

        One entry exists per real summary provider call. A provider failure or
        cancellation contributes ``{}`` because no usage was reported; the
        driver must still record one attempt for turn-budget accounting. A
        deterministic-only reduction contributes nothing.
        """
        drained = tuple(dict(record) for record in self._usage_records)
        self._usage_records.clear()
        return drained

    async def prepare(
        self,
        context: ExecutionContext,
        history: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolDefinition],
        *,
        force: bool = False,
        request_messages: Sequence[Mapping[str, Any]] | None = None,
        protected_user_text: str | None = None,
    ) -> ContextReduction | None:
        """Return smaller replacement messages, or ``None`` when no change is justified.

        ``protected_user_text`` is the current turn's original request text.
        The driver passes ``turn.request.text``. The policy finds every exact
        user-content match and uses the earliest one, so a repeated older text
        only keeps more history rather than dropping the current task or a
        later correction. If that match is the first user message there is no
        safe prefix left to summarize and the policy fails as ``None`` instead
        of claiming a summary.

        ``request_messages`` is optional. When the caller can assemble first, it
        passes the real ``bundle.messages`` and visible ``bundle.tools``; then
        ``before`` and ``after`` are measured over the actual request, including
        static system text, live runtime-context text and tool schema. The
        returned messages are always pure history: live runtime state is never
        summarized as if it were a user task, and the next request assembles
        fresh state. Without ``request_messages`` the older unit behavior uses
        the configured system plus ``request_overhead``.

        The caller owns the turn and persists the result. This method writes no
        session event and commits no checkpoint. ``force`` skips the pressure
        check once for a real overflow; it does not allow a second summary call
        or a result that does not shrink the estimate.
        """
        if not isinstance(context, ExecutionContext):
            raise TypeError("context must be an ExecutionContext")
        if not isinstance(force, bool):
            raise TypeError("force must be a bool")
        if protected_user_text is not None and not isinstance(protected_user_text, str):
            raise TypeError("protected_user_text must be a string or None")
        messages = self._copy_history(history)
        frozen_tools = self._copy_tools(tools)
        keep_user_turns, safe_to_summarize = self._resolve_keep_user_turns(
            messages, protected_user_text
        )
        request_snapshot = self._copy_request_messages(request_messages)
        if request_snapshot is None:
            overhead = self._overhead(context)
            before = self._request_estimate(messages, frozen_tools) + overhead
        else:
            overhead = 0
            before = self._estimate_exact(request_snapshot, frozen_tools)
        if not force and before <= self._limit:
            return None

        trimmed = self._shorten_tool_text(messages)
        trim_after = (
            self._future_estimate(
                messages, frozen_tools, request_snapshot, overhead
            )
            if trimmed
            else before
        )
        trim_reduction: ContextReduction | None = None
        if trimmed and trim_after < before:
            trim_reduction = ContextReduction(
                messages=tuple(messages),
                before_tokens=before,
                after_tokens=trim_after,
                reason="tool_text_trimmed",
            )
            if trim_after <= self._limit or not self._can_summarize(messages) or not safe_to_summarize:
                return trim_reduction

        if not self._can_summarize(messages) or not safe_to_summarize:
            return trim_reduction

        try:
            summary = await self._summarize(
                context,
                messages,
                frozen_tools,
                request_snapshot,
                overhead,
                keep_user_turns,
            )
        except (CompactionError, CheckpointConflictError) as exc:
            # A failed summary does not erase a real deterministic reduction.
            if trim_reduction is not None:
                return ContextReduction(
                    messages=trim_reduction.messages,
                    before_tokens=before,
                    after_tokens=trim_reduction.after_tokens,
                    reason=f"{trim_reduction.reason};summary_failed:{type(exc).__name__}",
                )
            return None

        projected, after = summary
        if after >= before:
            return trim_reduction
        if trim_reduction is not None and trim_reduction.after_tokens <= after:
            return trim_reduction
        return ContextReduction(
            messages=projected,
            before_tokens=before,
            after_tokens=after,
            reason=(
                "tool_text_trimmed+prefix_summarized"
                if trim_reduction is not None
                else "prefix_summarized"
            ),
        )

    async def preflight(
        self,
        session_id: str,
        *,
        tools: Sequence[ToolDefinition] = (),
        force: bool = False,
    ) -> ContextReduction | None:
        """Optional between-turn check that may commit a checkpoint.

        Use this only while no turn is running. The store's own unfinished-turn
        guard remains authoritative; a rejected commit leaves the projection
        untouched. When pressure is below the limit this performs no model call.
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        frozen_tools = self._copy_tools(tools)
        stored = await self._store.read_conversation(session_id)
        if not stored:
            return None
        history = self._copy_history(stored)
        if not history:
            return None
        before = self._request_estimate(history, frozen_tools)
        if not force and before <= self._limit:
            return None
        try:
            result = await self._call_compact(
                session_id, history, frozen_tools, commit_checkpoint=True
            )
        except (CompactionError, CheckpointConflictError):
            return None
        projected = self._project(result.checkpoint)
        after = self._request_estimate(projected, frozen_tools)
        if after >= before:
            return None
        return ContextReduction(
            messages=projected,
            before_tokens=before,
            after_tokens=after,
            reason="preflight_checkpoint",
        )

    # -- summary -------------------------------------------------------

    async def _summarize(
        self,
        context: ExecutionContext,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolDefinition],
        request_messages: Sequence[Mapping[str, Any]] | None,
        overhead: int,
        keep_user_turns: int,
    ) -> tuple[tuple[Mapping[str, Any], ...], int]:
        # CancelledError is a BaseException, so it propagates after the service
        # records the interrupted attempt instead of being turned into success.
        result = await self._call_compact(
            context.session_id,
            tuple(messages),
            tools,
            commit_checkpoint=False,
            keep_user_turns=keep_user_turns,
        )
        projected = self._project(result.checkpoint)
        after = self._future_estimate(projected, tools, request_messages, overhead)
        before = (
            self._estimate_exact(request_messages, tools)
            if request_messages is not None
            else self._request_estimate(messages, tools) + overhead
        )
        if after >= before:
            raise CompactionError("summary did not shrink the estimated request")
        return projected, after

    async def _call_compact(
        self,
        session_id: str,
        conversation: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolDefinition],
        *,
        commit_checkpoint: bool,
        keep_user_turns: int | None = None,
    ):
        return await compact_session(
            self._store,
            self._provider,
            session_id,
            model=self._model,
            effort=self._effort,
            provider_name=self._provider_name,
            max_tokens=self._max_tokens,
            keep_user_turns=(
                self._keep_user_turns if keep_user_turns is None else keep_user_turns
            ),
            max_retained_chars=self._max_retained_chars,
            max_summary_input_chars=self._max_summary_input_chars,
            max_message_chars=self._max_message_chars,
            context_window_tokens=self._capacity,
            reserve_tokens=self._reserve_tokens,
            token_estimator=self._prompt_estimate,
            request_token_estimator=lambda items: self._request_estimate(items, tools),
            conversation=conversation,
            commit_checkpoint=commit_checkpoint,
            usage_observer=self._record_usage,
        )

    def _record_usage(self, usage: Mapping[str, int]) -> None:
        record = {
            str(key): value
            for key, value in usage.items()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        }
        self._usage_records.append(record)

    @staticmethod
    def _project(checkpoint) -> tuple[dict[str, Any], ...]:
        return (
            {
                "role": "assistant",
                "content": f"{CHECKPOINT_SUMMARY_NOTICE}\n\n{checkpoint.summary}",
            },
            *(dict(message) for message in checkpoint.retained_messages),
        )

    # -- deterministic trim -------------------------------------------

    def _shorten_tool_text(self, messages: list[dict[str, Any]]) -> int:
        changed = 0
        for message in messages:
            if message.get("role") != "tool":
                continue
            content = message.get("content")
            if not isinstance(content, str) or len(content) < self._trim_min_chars:
                continue
            if _looks_visual(content):
                # A visual payload may carry the only frame the model needs;
                # never replace it with a made-up attachment path.
                continue
            replacement = self._tool_reference(content)
            if len(replacement) >= len(content) - _MIN_REFERENCE_SAVING:
                continue
            message["content"] = replacement
            changed += 1
        return changed

    @staticmethod
    def _tool_reference(content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        reference: dict[str, Any] = {
            "truncated_tool_text": {
                "sha256": digest,
                "original_chars": len(content),
                "full_text": "session event log",
            }
        }
        payload: Any
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, Mapping):
            kept: dict[str, Any] = {}
            for key in ("ok", *_STATE_KEYS, "error"):
                if key in payload:
                    kept[key] = _short_value(payload[key])
            refs: dict[str, Any] = {}
            safety: list[str] = []
            states: dict[str, Any] = {}
            _collect_evidence(payload, refs, safety, states)
            if states:
                kept.setdefault("state", states)
            if refs:
                kept["domain_refs"] = refs
            if safety:
                kept["safety"] = safety
            if kept:
                reference["kept"] = kept
        else:
            reference["excerpt"] = content[:_MAX_KEPT_TEXT]
        return json.dumps(reference, ensure_ascii=False)

    # -- helpers -------------------------------------------------------

    def _can_summarize(self, messages: Sequence[Mapping[str, Any]]) -> bool:
        user_positions = sum(1 for message in messages if message.get("role") == "user")
        return user_positions >= 2

    def _resolve_keep_user_turns(
        self, messages: Sequence[Mapping[str, Any]], protected_user_text: str | None
    ) -> tuple[int, bool]:
        """Keep the original current task and every later correction.

        The scan is exact content matching over user messages in order. The
        earliest match wins, which can keep extra history when an old turn had
        the same text, but never drops the current task. If that match is the
        first user message, no safe prefix remains to summarize.
        """
        if protected_user_text is None:
            return self._keep_user_turns, True
        positions = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "user"
        ]
        match = next(
            (
                index
                for index in positions
                if messages[index].get("content") == protected_user_text
            ),
            None,
        )
        if match is None:
            return self._keep_user_turns, True
        keep = sum(1 for index in positions if index >= match)
        if positions and positions[0] == match:
            return keep, False
        return max(self._keep_user_turns, keep), True

    @staticmethod
    def _copy_history(history: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if not history:
            raise ValueError("history must not be empty")
        copied: list[dict[str, Any]] = []
        for message in history:
            if not isinstance(message, Mapping):
                raise TypeError("history messages must be mappings")
            if is_runtime_context_message(message):
                # Live state is never conversation: it must not enter a summary,
                # a retained tail, or a returned replacement history.
                continue
            copied.append(json.loads(json.dumps(dict(message), ensure_ascii=False, default=str)))
        if not copied:
            raise ValueError("history must contain at least one non-runtime message")
        return copied

    @staticmethod
    def _copy_tools(tools: Sequence[ToolDefinition]) -> tuple[ToolDefinition, ...]:
        copied = tuple(tools)
        if any(not isinstance(tool, ToolDefinition) for tool in copied):
            raise TypeError("tools must contain ToolDefinition instances")
        return copied

    @staticmethod
    def _copy_request_messages(
        request_messages: Sequence[Mapping[str, Any]] | None,
    ) -> tuple[Mapping[str, Any], ...] | None:
        if request_messages is None:
            return None
        if isinstance(request_messages, (str, bytes)):
            raise TypeError("request_messages must be a sequence of mappings")
        copied: list[Mapping[str, Any]] = []
        for message in request_messages:
            if not isinstance(message, Mapping):
                raise TypeError("request_messages must contain mappings")
            copied.append(dict(message))
        if not copied:
            raise ValueError("request_messages must not be empty")
        return tuple(copied)

    def _request_estimate(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[ToolDefinition]
    ) -> int:
        request_messages = list(messages)
        if self._system_instructions.strip():
            request_messages.insert(0, {"role": "system", "content": self._system_instructions})
        return self._estimate_exact(request_messages, tools)

    def _estimate_exact(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[ToolDefinition]
    ) -> int:
        """Measure messages exactly as supplied; no system text is inserted."""
        if self._estimator is not None:
            tokens = self._estimator(messages, tools)
        else:
            provider_tools = [tool.to_provider_dict() for tool in tools]
            tokens, _source = estimate_prompt_tokens_chain(
                self._provider, self._model, list(messages), provider_tools
            )
            if not tokens:
                tokens = _character_estimate(messages, provider_tools)
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            raise ValueError("estimator must return a non-negative int")
        return tokens

    def _future_estimate(
        self,
        history: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolDefinition],
        request_messages: Sequence[Mapping[str, Any]] | None,
        overhead: int,
    ) -> int:
        """Estimate the next assembled request for a reduced pure history.

        With a real request snapshot, keep its system messages and runtime tail
        but replace the conversation part with ``history``. That counts static
        and live text once, without adding the configured system twice.
        """
        if request_messages is None:
            return self._request_estimate(history, tools) + overhead
        systems = [
            dict(message)
            for message in request_messages
            if message.get("role") == "system"
        ]
        runtime = [
            dict(message)
            for message in request_messages
            if is_runtime_context_message(message)
        ]
        return self._estimate_exact([*systems, *history, *runtime], tools)

    def _prompt_estimate(self, messages: Sequence[Mapping[str, Any]]) -> int:
        """Measure a standalone summary prompt, without the next-request system."""
        if self._estimator is not None:
            tokens = self._estimator(list(messages), ())
        else:
            tokens, _source = estimate_prompt_tokens_chain(
                self._provider, self._model, list(messages), None
            )
            if not tokens:
                tokens = _character_estimate(messages, ())
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            raise ValueError("estimator must return a non-negative int")
        return tokens

    def _overhead(self, context: ExecutionContext) -> int:
        if self._request_overhead is None:
            return 0
        tokens = self._request_overhead(context)
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            raise ValueError("request_overhead must return a non-negative int")
        return tokens


# -- reference extraction ---------------------------------------------


def _short_value(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _MAX_KEPT_TEXT else value[:_MAX_KEPT_TEXT] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= _MAX_KEPT_TEXT else text[:_MAX_KEPT_TEXT] + "…"


def _collect_evidence(
    node: Any,
    refs: dict[str, Any],
    safety: list[str],
    states: dict[str, Any],
    depth: int = 0,
) -> None:
    if depth > 8:
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            lowered = str(key).lower()
            if lowered in _REF_KEYS and value not in _EMPTY and lowered not in refs:
                refs[lowered] = _short_value(value)
            if (
                any(
                    lowered == state_key or lowered.endswith("_" + state_key)
                    for state_key in _STATE_KEYS
                )
                and value not in _EMPTY
                and lowered not in states
            ):
                states[lowered] = _short_value(value)
            if any(hint in lowered for hint in _SAFETY_HINTS) and value not in _EMPTY:
                item = f"{key}: {_short_value(value)}"
                if item not in safety and len(safety) < 8:
                    safety.append(item)
            if lowered == "error" and value not in _EMPTY:
                item = f"error: {_short_value(value)}"
                if item not in safety:
                    safety.append(item)
            _collect_evidence(value, refs, safety, states, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _collect_evidence(item, refs, safety, states, depth + 1)


def _looks_visual(content: str) -> bool:
    if "data:image" in content.lower():
        return True
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    return _has_visual_value(payload)


def _has_visual_value(node: Any, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(node, Mapping):
        for key, value in node.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in _VISUAL_HINTS) and value not in _EMPTY:
                return True
            if _has_visual_value(value, depth + 1):
                return True
        return False
    if isinstance(node, list):
        return any(_has_visual_value(item, depth + 1) for item in node)
    return False


def _character_estimate(
    messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
) -> int:
    chars = 0
    for message in messages:
        chars += len(str(message.get("role") or ""))
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif content is not None:
            chars += len(json.dumps(content, ensure_ascii=False, default=str))
        for key in ("tool_calls", "tool_call_id", "name"):
            value = message.get(key)
            if value is not None:
                chars += len(json.dumps(value, ensure_ascii=False, default=str))
    for tool in tools:
        chars += len(json.dumps(tool, ensure_ascii=False, default=str))
    return max(1, math.ceil(chars / 4))
