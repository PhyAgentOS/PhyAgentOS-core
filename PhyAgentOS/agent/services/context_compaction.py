"""Manual context compaction over the append-only session store.

This service is the only writer of context checkpoints. It never rewrites
``session_events`` and never writes ``context.compacted`` (that event belongs to
the loop driver's turn). A successful run adds one projection checkpoint:
summary plus the complete recent messages kept verbatim. ``read_conversation``
then starts from the checkpoint instead of replaying the whole log.

Rules enforced here:

* compaction is manual; nothing schedules it, and a summary is never turned
  into a user task or executed;
* the summary request reuses the caller's current provider/model/effort through
  ``chat_with_retry``. This module imports no provider or Forge module;
* every summary call is tracked as a compaction attempt before the request is
  sent. Known usage is recorded even when the summary is rejected, the result
  would not shrink the history, or the checkpoint loses the compare-and-swap,
  so cost is visible without changing the conversation projection. Unknown
  token kinds are left out rather than stored as fake zeros;
* the retained tail starts at a user-message boundary, so tool calls and their
  results stay together in one complete turn;
* the summary input is bounded before the call by characters and, when the
  caller supplies the model's context window, by estimated tokens plus the
  output reserve. The first user task and its constraints are always kept;
  only older middle groups may be omitted, and that omission is reported in the
  result warnings and in the summary prompt. Oversized tool output is shortened
  for the summary prompt. The kept tail is never split; if it is too large for
  its budget, compaction fails instead of claiming success;
* the checkpoint is committed with a compare-and-swap on the session head. If
  any event arrived after the snapshot, or the session has unfinished work, the
  commit is rejected and the original history and projection remain unchanged;
* a summary that would not make the projected history smaller is rejected.

Forge blockers are supplied to the model by the live prompt fragment, not by
this summary, so the summary neither copies nor changes them.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.model import ChatProvider
from ..core.session import (
    CHECKPOINT_SUMMARY_NOTICE,
    CheckpointConflictError,
    CompactionAttempt,
    ContextCheckpoint,
    SQLiteSessionStore,
)

__all__ = ["CompactionError", "CompactionResult", "compact_session"]

_SUMMARY_SYSTEM_PROMPT = """\
You compress an earlier part of an agent conversation so the session can keep \
going. Write one compact summary in Markdown with these sections:

## Objective
What the user asked for, including constraints and acceptance criteria.

## Decisions
Choices already made and why; include things that must not be changed.

## Completed
Work that is done, with concrete outcomes.

## In progress / Next move
What remains, in order, and the exact next step.

## Blockers
Unresolved problems and what would resolve them.

## Relevant files and tasks
Paths and task references that matter. Keep identifiers exactly as written.

Only use what the transcript states. Do not invent user intent, do not claim \
work succeeded unless the transcript shows it, and do not repeat credentials. \
Output only the summary. Recent messages are kept verbatim beside it, so focus \
on the older part, especially the first task and its constraints.\
"""

# finish reasons that mean the answer is absent or cut short, not a summary.
_REJECTED_FINISH_REASONS = frozenset({"error", "length", "content_filter"})
_MIN_MESSAGE_CHARS = 200
_MIN_SUMMARY_BUDGET = 200


class CompactionError(RuntimeError):
    """Compaction was refused or failed; no checkpoint was written."""


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """What one successful compaction observed and stored."""

    session_id: str
    checkpoint: ContextCheckpoint
    attempt: CompactionAttempt
    before_tokens: int
    after_tokens: int
    usage: Mapping[str, int] = field(default_factory=dict)
    omitted_prefix_messages: int = 0
    truncated_prefix_messages: int = 0
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        usage: dict[str, int] = {}
        for key, value in self.usage.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"usage[{key!r}] must be a non-negative int")
            usage[str(key)] = value
        object.__setattr__(self, "usage", usage)
        if self.before_tokens < 0 or self.after_tokens < 0:
            raise ValueError("token estimates must be >= 0")
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings))


@dataclass(frozen=True, slots=True)
class _Plan:
    prefix: tuple[Mapping[str, Any], ...]
    retained: tuple[Mapping[str, Any], ...]


async def compact_session(
    store: SQLiteSessionStore,
    provider: ChatProvider,
    session_id: str,
    *,
    model: str,
    effort: str | None = None,
    provider_name: str | None = None,
    max_tokens: int | None = None,
    keep_user_turns: int = 2,
    max_retained_chars: int = 40_000,
    max_summary_input_chars: int = 60_000,
    max_message_chars: int = 4_000,
    context_window_tokens: int | None = None,
    reserve_tokens: int | None = None,
    token_estimator: Callable[[Sequence[Mapping[str, Any]]], int] | None = None,
    request_token_estimator: Callable[[Sequence[Mapping[str, Any]]], int] | None = None,
    conversation: Sequence[Mapping[str, Any]] | None = None,
    commit_checkpoint: bool = True,
    usage_observer: Callable[[Mapping[str, int]], None] | None = None,
) -> CompactionResult:
    """Summarize the older conversation and store one checkpoint.

    ``model``/``effort``/``provider_name`` must be the session's current
    selection; they are forwarded to the provider and recorded on the attempt
    and checkpoint. ``context_window_tokens`` is the selected model's context
    window; when given, the summary prompt plus ``reserve_tokens`` (defaults to
    ``max_tokens``) must fit before any network call. ``token_estimator`` may
    wrap provider-specific token counting; the default is a character estimate.
    ``request_token_estimator`` must measure a *next request*: the caller adds
    the same system prompt and visible tool definitions the live status line
    uses. It defaults to ``token_estimator`` so before/after are always measured
    by one method; success is committed only when that real next request is
    smaller, never when a character count merely shrank.

    ``conversation`` optionally supplies the messages to compact instead of
    reading the store projection. This is how an in-turn policy summarizes the
    current history, which is not yet in ``read_conversation``. With
    ``commit_checkpoint=False`` the summary call is still recorded as an
    attempt and the projected messages are returned, but no checkpoint is
    written; that keeps the store's unfinished-turn protection untouched. The
    driver that owns the turn persists the returned messages in its own
    ``context.compacted`` event.

    ``usage_observer`` is called exactly once for every real summary provider
    call with the usage that call reported. A provider failure or cancellation
    reports ``{}`` because no usage was received; it is still one call for
    budget accounting. The observer never replaces the durable attempt row.

    The caller serializes calls with the session's turn queue so no turn is
    active. Raises :class:`CompactionError` when compaction is refused or the
    provider fails, and
    :class:`~PhyAgentOS.agent.core.session.CheckpointConflictError` when the log
    changed after the snapshot. In every failure case no checkpoint is written;
    usage already reported by the provider is kept on the attempt row.
    """
    if not isinstance(store, SQLiteSessionStore):
        raise TypeError("store must be a SQLiteSessionStore")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise ValueError("effort must be a non-empty string or None")
    if (
        keep_user_turns < 1
        or max_retained_chars < 1
        or max_summary_input_chars < 1
        or max_message_chars < 1
    ):
        raise ValueError("compaction budgets must be positive")
    if max_tokens is not None and max_tokens < 1:
        raise ValueError("max_tokens must be positive or None")
    if context_window_tokens is not None and context_window_tokens < 1:
        raise ValueError("context_window_tokens must be positive or None")
    if reserve_tokens is not None and reserve_tokens < 0:
        raise ValueError("reserve_tokens must be >= 0 or None")
    if not isinstance(commit_checkpoint, bool):
        raise TypeError("commit_checkpoint must be a bool")
    if usage_observer is not None and not callable(usage_observer):
        raise TypeError("usage_observer must be callable or None")

    estimator = token_estimator or _estimate_tokens
    request_estimator = request_token_estimator or estimator

    # Read the head first: if anything is appended after this point, the commit
    # CAS below rejects the checkpoint instead of covering unseen work.
    head = await store.read_head_sequence(session_id)
    if conversation is None:
        conversation = tuple(await store.read_conversation(session_id))
    else:
        if not isinstance(conversation, Sequence) or isinstance(conversation, (str, bytes)):
            raise TypeError("conversation must be a sequence of mappings")
        conversation = tuple(dict(message) for message in conversation)
    if not conversation:
        raise CompactionError("session has no complete conversation to compact")

    plan = _plan(conversation, keep_user_turns, max_retained_chars)
    _check_tool_pairing(plan.prefix, "summarized prefix")
    _check_tool_pairing(plan.retained, "retained messages")

    budget = max_summary_input_chars
    reserve = reserve_tokens if reserve_tokens is not None else (max_tokens or 0)
    while True:
        transcript, omitted, truncated = _bounded_transcript(
            plan.prefix,
            budget=budget,
            max_message_chars=min(max_message_chars, budget),
        )
        if not transcript.strip():
            raise CompactionError("nothing is left to summarize within the input budget")
        prompt = _summary_prompt(transcript, omitted)
        if context_window_tokens is None:
            break
        prompt_tokens = _checked_estimate(estimator, prompt)
        if prompt_tokens + reserve <= context_window_tokens:
            break
        if budget <= _MIN_SUMMARY_BUDGET:
            raise CompactionError(
                "summary prompt exceeds the model context before sending "
                f"(~{prompt_tokens} tokens + {reserve} reserved > "
                f"{context_window_tokens})"
            )
        budget = max(_MIN_SUMMARY_BUDGET, budget // 2)

    warnings: list[str] = []
    if omitted:
        warnings.append(
            f"{omitted} older messages were omitted from the summary input; "
            "the first task and its constraints were kept"
        )
    if truncated:
        warnings.append(f"{truncated} messages were shortened in the summary input")

    attempt = await store.begin_compaction_attempt(
        session_id, provider=provider_name, model=model, effort=effort
    )
    generation: dict[str, Any] = {"model": model}
    if effort is not None:
        generation["reasoning_effort"] = effort
    if max_tokens is not None:
        generation["max_tokens"] = max_tokens

    try:
        response = await provider.chat_with_retry(
            messages=prompt, tools=None, **generation
        )
    except asyncio.CancelledError as exc:
        observed = _usage(exc)
        await _finish_attempt(store, attempt, "interrupted", observed, "cancelled")
        if usage_observer is not None:
            usage_observer(observed)
        raise
    except Exception as exc:  # provider failures are not summaries
        reason = f"provider_error:{type(exc).__name__}"
        observed = _usage(exc)
        await _finish_attempt(store, attempt, "failed", observed, reason)
        if usage_observer is not None:
            usage_observer(observed)
        raise CompactionError(f"summary provider call failed: {type(exc).__name__}") from exc

    usage = _usage(response)
    if usage_observer is not None:
        usage_observer(dict(usage))
    try:
        summary = _summary_text(response)
        before_tokens = _checked_estimate(request_estimator, conversation)
        projected = (
            {
                "role": "assistant",
                "content": f"{CHECKPOINT_SUMMARY_NOTICE}\n\n{summary}",
            },
            *plan.retained,
        )
        after_tokens = _checked_estimate(request_estimator, projected)
        if after_tokens >= before_tokens:
            raise CompactionError(
                "compaction would not shrink the conversation "
                f"(before ~{before_tokens} tokens, after ~{after_tokens})"
            )
        checkpoint = ContextCheckpoint(
            session_id=session_id,
            covers_sequence=head,
            summary=summary,
            retained_messages=plan.retained,
            selection={"provider": provider_name, "model": model, "effort": effort},
            usage=usage,
        )
        committed = (
            await store.commit_checkpoint(checkpoint, expected_head_sequence=head)
            if commit_checkpoint
            else checkpoint
        )
    except asyncio.CancelledError:
        await _finish_attempt(store, attempt, "interrupted", usage, "cancelled")
        raise
    except CheckpointConflictError as exc:
        await _finish_attempt(store, attempt, "failed", usage, str(exc))
        raise
    except BaseException as exc:
        await _finish_attempt(store, attempt, "failed", usage, _reason(exc))
        raise

    assert attempt.attempt_id is not None
    finished = await store.finish_compaction_attempt(
        attempt.attempt_id, status="completed", usage=usage
    )
    return CompactionResult(
        session_id=session_id,
        checkpoint=committed,
        attempt=finished,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        usage=usage,
        omitted_prefix_messages=omitted,
        truncated_prefix_messages=truncated,
        warnings=tuple(warnings),
    )


# -- attempts ---------------------------------------------------------


async def _finish_attempt(
    store: SQLiteSessionStore,
    attempt: CompactionAttempt,
    status: str,
    usage: Mapping[str, int],
    reason: str | None,
) -> None:
    assert attempt.attempt_id is not None
    await store.finish_compaction_attempt(
        attempt.attempt_id, status=status, usage=usage, reason=reason
    )


def _reason(exc: BaseException) -> str:
    """A short, credential-free reason for the attempt row."""
    if isinstance(exc, CompactionError):
        return str(exc)
    return type(exc).__name__


# -- planning ---------------------------------------------------------


def _user_groups(messages: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Split messages into groups that each start at a user message."""
    groups: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "user" and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return groups


def _plan(
    conversation: Sequence[Mapping[str, Any]],
    keep_user_turns: int,
    max_retained_chars: int,
) -> _Plan:
    user_positions = [
        index for index, message in enumerate(conversation) if message.get("role") == "user"
    ]
    if len(user_positions) < 2:
        raise CompactionError(
            "need at least two complete user turns before compacting"
        )
    # Always leave at least one user turn outside the summary. With two turns
    # this compacts the older one, which is the only prefix that can be removed.
    keep = min(keep_user_turns, len(user_positions) - 1)
    tail_start = user_positions[-keep]
    prefix = tuple(conversation[:tail_start])
    retained = tuple(conversation[tail_start:])
    if not prefix:
        raise CompactionError("no older conversation to compact")
    if retained[0].get("role") != "user":
        raise CompactionError("retained messages do not start at a user turn")

    retained_chars = sum(_message_chars(message) for message in retained)
    if retained_chars > max_retained_chars:
        raise CompactionError(
            "recent turns exceed the retained budget "
            f"({retained_chars} > {max_retained_chars} characters); "
            "nothing can be kept without splitting a turn"
        )
    return _Plan(prefix=prefix, retained=retained)


def _message_chars(message: Mapping[str, Any]) -> int:
    total = len(str(message.get("role") or ""))
    content = message.get("content")
    if isinstance(content, str):
        total += len(content)
    elif content is not None:
        total += len(json.dumps(content, ensure_ascii=False, default=str))
    for key in ("tool_calls", "tool_call_id", "name"):
        value = message.get(key)
        if value is not None:
            total += len(json.dumps(value, ensure_ascii=False, default=str))
    return total


def _estimate_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Last-resort character estimate used when the caller supplies none."""
    if not messages:
        return 0
    return max(1, math.ceil(sum(_message_chars(message) for message in messages) / 4))


def _checked_estimate(
    estimator: Callable[[Sequence[Mapping[str, Any]]], int],
    messages: Sequence[Mapping[str, Any]],
) -> int:
    value = estimator(messages)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("token_estimator must return a non-negative int")
    return value


# -- bounded summary input --------------------------------------------


def _render_message(message: Mapping[str, Any], limit: int) -> tuple[str, bool]:
    role = message.get("role")
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif content is None:
        text = ""
    else:
        text = json.dumps(content, ensure_ascii=False, default=str)
    truncated = len(text) > limit
    if truncated:
        text = text[:limit] + "…[truncated]"
    parts = [f"[{role}] {text}"]
    tool_calls = message.get("tool_calls")
    if tool_calls:
        parts.append("tool_calls: " + _short_json(tool_calls, limit))
    tool_call_id = message.get("tool_call_id")
    if tool_call_id:
        parts.append(f"tool_call_id: {tool_call_id}")
    return "\n".join(parts), truncated


def _short_json(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def _bounded_transcript(
    messages: Sequence[Mapping[str, Any]],
    *,
    budget: int,
    max_message_chars: int,
) -> tuple[str, int, int]:
    """Render the prefix within ``budget``, dropping only older middle groups.

    The first user group is always kept, so the original task and its
    constraints are never dropped just to save room. Returns
    ``(transcript, omitted_message_count, truncated_message_count)``; the call
    site must surface the omissions instead of presenting a bounded summary as
    complete.
    """
    groups = _user_groups(messages)
    if not groups:
        return "", 0, 0
    first = groups[0]
    rest_start = 1
    omitted = 0
    per_message = min(max_message_chars, budget)
    while True:
        selected = [
            *first,
            *(message for group in groups[rest_start:] for message in group),
        ]
        rendered: list[str] = []
        truncated = 0
        for message in selected:
            text, was_truncated = _render_message(message, per_message)
            rendered.append(text)
            truncated += int(was_truncated)
        transcript = "\n\n".join(rendered)
        if len(transcript) <= budget:
            return transcript, omitted, truncated
        if rest_start + 1 < len(groups):
            omitted += len(groups[rest_start])
            rest_start += 1
            continue
        if per_message > _MIN_MESSAGE_CHARS:
            per_message = max(_MIN_MESSAGE_CHARS, per_message // 2)
            continue
        raise CompactionError(
            "summary input exceeds its budget even after shortening tool output "
            "and keeping the first task"
        )


def _summary_prompt(transcript: str, omitted: int) -> list[dict[str, Any]]:
    note = ""
    if omitted:
        note = (
            f"\n\nNote: {omitted} older middle messages were omitted because the "
            "input exceeded its size budget. The transcript starts with the first "
            "task and its constraints; do not guess the omitted content."
        )
    return [
        {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Conversation to compact:\n\n{transcript}{note}",
        },
    ]


# -- validation -------------------------------------------------------


def _check_tool_pairing(
    messages: Sequence[Mapping[str, Any]], where: str
) -> None:
    """Reject a projection that would keep a tool result without its call."""
    declared: set[str] = set()
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or ():
                if isinstance(call, Mapping):
                    call_id = call.get("id")
                    if isinstance(call_id, str):
                        declared.add(call_id)
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in declared:
                raise CompactionError(
                    f"{where} contains a tool result without its tool call"
                )


def _summary_text(response: object) -> str:
    finish_reason = getattr(response, "finish_reason", None)
    if not isinstance(finish_reason, str) or not finish_reason.strip():
        raise CompactionError("summary response has no finish_reason")
    if finish_reason in _REJECTED_FINISH_REASONS:
        raise CompactionError(
            f"summary response was not complete (finish_reason={finish_reason})"
        )
    content = getattr(response, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise CompactionError("summary response has no text")
    return content.strip()


def _usage(response: object) -> dict[str, int]:
    raw = getattr(response, "usage", None)
    if not isinstance(raw, Mapping):
        return {}
    usage: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[str(key)] = value
    return usage
