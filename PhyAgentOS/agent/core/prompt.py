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
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .types import ExecutionContext, PromptBundle, ToolDefinition

__all__ = ["PromptAssemblerImpl", "PromptFragment"]

PromptFragment = Callable[[ExecutionContext], str | None]


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
    ) -> None:
        if not isinstance(system_instructions, str):
            raise TypeError("system_instructions must be a string")
        frozen_fragments = tuple(fragments)
        for fragment in frozen_fragments:
            if not callable(fragment):
                raise TypeError("fragments must be callables returning str | None")
        self._system_instructions = system_instructions
        self._fragments = frozen_fragments

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

        system_text = self._system_text(context)
        if system_text and not _already_carries_system_text(messages, system_text):
            messages.insert(0, {"role": "system", "content": system_text})

        visible = tuple(tool for tool in tools if context.policy.allows(tool.name))
        return PromptBundle(messages=tuple(messages), tools=visible)

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
