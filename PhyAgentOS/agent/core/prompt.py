"""Deterministic prompt assembly for one step of the agent loop.

The assembler owns message layout only: it puts one system message built from
static instructions plus caller-supplied fragments in front of a deep copy of
the history the loop driver built, then narrows the offered tool list with the
step policy. It keeps no session state, reads no store, and never asks a domain
service for text.

Fragments are read on every ``assemble`` call and receive that step's
``ExecutionContext`` as their only argument, so text can depend on the session
or step without module globals or context variables. What they return is live
information for that step; deciding whether it may be cached, and for how long,
belongs to the domain code that supplies the fragment, not to the core.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .types import ExecutionContext, PromptBundle, ToolDefinition, ToolPolicy

__all__ = [
    "PromptAssemblerImpl",
    "PromptFragment",
    "ToolVisibility",
    "project_forge_tool_results",
    "project_skill_documents",
]

PromptFragment = Callable[[ExecutionContext], str | None]
ToolVisibility = Callable[[ExecutionContext], "frozenset[str] | None"]  # hidden tool names



def _copy_history(history: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not history:
        raise ValueError("history must not be empty")
    messages: list[dict[str, Any]] = []
    for message in history:
        if not isinstance(message, Mapping):
            raise TypeError("history messages must be mappings")
        messages.append(copy.deepcopy(dict(message)))
    return messages


def _already_carries_system_text(
    messages: Sequence[Mapping[str, Any]], system_text: str
) -> bool:
    if not messages or messages[0].get("role") != "system":
        return False
    content = messages[0].get("content")
    return isinstance(content, str) and system_text in content


_SKILL_TOOL = "activate_skill"
_FORGE_RESULT_TOOLS = frozenset(
    {
        "forge_task_get",
        "forge_task_create",
        "forge_task_begin_revision",
        "forge_task_finalize",
        "forge_tool_context",
        "forge_tool_query",
        "forge_tool_action_status",
        "forge_tool_action_result",
        "forge_tool_session_status",
        "forge_tool_session_result",
    }
)
_SUCCESS_STATES = frozenset(
    {"succeeded", "completed", "complete", "done", "stopped", "ok", "success"}
)
_STATE_KEYS = (
    "status", "state", "terminal", "phase", "action_status", "session_status",
    "cancel_status", "cancellation_status", "domain_status",
)
_UNSAFE_TRUE_KEYS = frozenset({"accepted", "unknown", "partial", "error"})
_MIN_DEDUP_SAVING = 128


def _parse_json_object(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def project_skill_documents(messages: list[dict[str, Any]]) -> int:
    """Keep one full copy per (Skill name, exact document hash) in one request.

    Only a parsed ``activate_skill`` result that still carries the full
    ``skill`` text counts as a copy; summaries, checkpoints or truncated text
    never do. Different versions of the same Skill are different documents and
    each keeps one full body, so older text is never silently replaced by a
    marker claiming it was the same document. The newest copy of each
    (name, hash) pair is kept; earlier identical copies keep their activation
    metadata and a hash marker. Message roles, tool call ids and ordering are
    untouched, and durable events are never modified.
    """
    newest: dict[tuple[str, str], int] = {}
    parsed: dict[int, tuple[dict[str, Any], str, str, str]] = {}
    for index, message in enumerate(messages):
        if message.get("role") != "tool" or message.get("name") != _SKILL_TOOL:
            continue
        payload = _parse_json_object(message.get("content"))
        if payload is None:
            continue
        skill = payload.get("skill")
        activation = payload.get("activation")
        activation = activation if isinstance(activation, Mapping) else {}
        name = activation.get("skill_name") or payload.get("skill_name")
        if not isinstance(skill, str) or not skill.strip() or not isinstance(name, str):
            continue
        digest = hashlib.sha256(skill.encode("utf-8")).hexdigest()
        parsed[index] = (payload, name, skill, digest)
        newest[(name, digest)] = index

    omitted = 0
    for index, (payload, name, skill, digest) in parsed.items():
        if newest[(name, digest)] == index:
            continue
        original = messages[index]["content"]
        projected = copy.deepcopy(payload)
        projected.pop("skill", None)
        projected["skill_document_omitted"] = {"sha256": digest}
        replacement = json.dumps(projected, ensure_ascii=False)
        if len(original) - len(replacement) < _MIN_DEDUP_SAVING:
            # Never grow a request, and do not trade a short body for a marker.
            continue
        messages[index]["content"] = replacement
        omitted += 1
    return omitted


def _contains_unsafe_state(node: Any, depth: int = 0) -> bool:
    """Any nested error / accepted / unknown / non-success state blocks dedup."""
    if depth > 8:
        return True
    if isinstance(node, Mapping):
        for key, value in node.items():
            lowered = str(key).lower()
            if lowered in _UNSAFE_TRUE_KEYS and value not in (None, False, "", 0, [], {}):
                return True
            if "error" in lowered and value not in (None, False, "", 0, [], {}):
                return True
            if any(lowered == state_key or lowered.endswith("_" + state_key)
                   for state_key in _STATE_KEYS):
                if isinstance(value, str) and value and value.lower() not in _SUCCESS_STATES:
                    return True
            if _contains_unsafe_state(value, depth + 1):
                return True
        return False
    if isinstance(node, list):
        return any(_contains_unsafe_state(item, depth + 1) for item in node)
    return False


def project_forge_tool_results(messages: list[dict[str, Any]]) -> int:
    """Drop only older *identical* successful Forge results in one request.

    The newest copy of each exact (tool name, content hash) stays in full. No
    size-based trimming is performed: opaque query/context/action/session
    payloads, failures, unknown/accepted/partial states and anything carrying a
    nested error keep their full text. A replaced copy keeps only the full hash
    marker, and only when that serialization is strictly shorter than the text
    it replaces; short results are left verbatim.
    """
    newest: dict[tuple[str, str], int] = {}
    candidates: dict[int, tuple[str, str]] = {}
    for index, message in enumerate(messages):
        name = message.get("name")
        content = message.get("content")
        if message.get("role") != "tool" or name not in _FORGE_RESULT_TOOLS:
            continue
        if not isinstance(content, str) or not content:
            continue
        payload = _parse_json_object(content)
        if payload is None or payload.get("ok") is not True:
            continue
        if _contains_unsafe_state(payload):
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        key = (name, digest)
        candidates[index] = (name, digest)
        newest[key] = index

    replaced = 0
    for index, (name, digest) in candidates.items():
        if newest[(name, digest)] == index:
            continue
        replacement = json.dumps(
            {
                "ok": True,
                "duplicate_of_later_result": {"tool": name, "sha256": digest},
            },
            ensure_ascii=False,
        )
        if len(messages[index]["content"]) - len(replacement) < _MIN_DEDUP_SAVING:
            # Never grow a request, and do not trade a short result for a marker.
            continue
        messages[index]["content"] = replacement
        replaced += 1
    return replaced


class PromptAssemblerImpl:
    """Builds one step's :class:`PromptBundle` from injected inputs.

    ``system_instructions`` is the fixed text placed in the system message on
    every call; an empty or whitespace-only value adds nothing on its own.
    ``fragments`` take the current step's :class:`ExecutionContext` and are
    evaluated once per ``assemble`` call, in order, each returning a text
    fragment or ``None``. They are read live for every step and are never
    stored or memoized here, so two concurrent sessions get their own text
    instead of sharing global state. Caching is the fragment owner's decision.
    Exceptions raised by a fragment propagate unchanged, so the caller sees a
    failed assembly instead of a silently shortened prompt.
    """

    def __init__(
        self,
        system_instructions: str,
        *,
        fragments: Sequence[PromptFragment] = (),
        hidden_tools: ToolVisibility | None = None,
    ) -> None:
        if not isinstance(system_instructions, str):
            raise TypeError("system_instructions must be a string")
        frozen_fragments = tuple(fragments)
        for fragment in frozen_fragments:
            if not callable(fragment):
                raise TypeError("fragments must be callables returning str | None")
        if hidden_tools is not None and not callable(hidden_tools):
            raise TypeError("hidden_tools must be callable or None")
        self._system_instructions = system_instructions
        self._fragments = frozen_fragments
        self._hidden_tools = hidden_tools

    async def assemble(
        self,
        context: ExecutionContext,
        *,
        history: Sequence[Mapping[str, Any]],
        tools: Sequence[ToolDefinition],
    ) -> PromptBundle:
        """Return the system-first messages and the policy-visible tools.

        ``history`` is the driver's per-turn conversation; it is deep-copied,
        so later caller mutations cannot change the returned bundle. This
        class does not validate conversation state, so it does not require a
        user message or a settled tool message. The step policy may hide an
        offered tool but can never expose one the caller did not offer.
        """
        if not isinstance(context, ExecutionContext):
            raise TypeError("context must be an ExecutionContext")
        messages = _copy_history(history)
        project_forge_tool_results(messages)
        project_skill_documents(messages)

        system_text = self._system_text(context)
        if system_text and not _already_carries_system_text(messages, system_text):
            messages.insert(0, {"role": "system", "content": system_text})

        hidden_names: frozenset[str] = frozenset()
        if self._hidden_tools is not None:
            hidden = self._hidden_tools(context)
            if hidden:
                hidden_names = frozenset(hidden)
        visible = tuple(
            tool
            for tool in tools
            if context.policy.allows(tool.name) and tool.name not in hidden_names
        )
        effective_policy = None
        if self._hidden_tools is not None:
            # Executor and prompt share one effective policy. The allowed set is
            # exactly the visible subset, so a hidden tool called by name is
            # rejected even if the model fabricates the call; the caller's
            # denied set is preserved and the registration range is unchanged.
            effective_policy = ToolPolicy(
                allowed_tools=frozenset(tool.name for tool in visible),
                denied_tools=context.policy.denied_tools,
            )
        return PromptBundle(messages=tuple(messages), tools=visible, policy=effective_policy)

    def _system_text(self, context: ExecutionContext) -> str:
        parts: list[str] = []
        if self._system_instructions.strip():
            parts.append(self._system_instructions)
        for fragment in self._fragments:
            text = fragment(context)
            if text is None:
                continue
            if not isinstance(text, str):
                raise TypeError("prompt fragment must return a string or None")
            if text.strip():
                parts.append(text)
        return "\n\n".join(parts)
