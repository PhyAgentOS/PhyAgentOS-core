"""On-demand tool discovery over an already-built registration list.

The capability adds one small always-visible search tool and hides only
low-frequency candidate tools (by default, names starting with ``mcp_``).
Basic tools and Forge tools are never candidates unless the caller explicitly
passes their prefix.

Discovery is per session and per schema only: a matched tool becomes part of
the next step's visible set, but the normal policy still decides whether it may
execute. A later hand-crafted or programmatic call does not bypass that check,
and another session never inherits this session's discoveries.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

from PhyAgentOS.agent.core.types import (
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)

__all__ = ["ToolSearchCapability"]

_SEARCH_PARAMETERS: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Words from a tool name or description.",
            "minLength": 1,
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of matching tools.",
            "minimum": 1,
            "maximum": 20,
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_DEFAULT_CANDIDATE_PREFIXES = ("mcp_",)
_NEVER_CANDIDATE_PREFIXES = ("forge_",)
_NEVER_CANDIDATE_NAMES = frozenset({"tool_search"})


class ToolSearchCapability:
    """Expose a search tool and hide candidate schemas until searched."""

    def __init__(
        self,
        registrations: Sequence[ToolRegistration] = (),
        *,
        candidate_prefixes: Sequence[str] = _DEFAULT_CANDIDATE_PREFIXES,
        tool_name: str = "tool_search",
        max_matches: int = 8,
        max_catalog_entries: int = 32,
        max_sessions: int = 1024,
    ) -> None:
        if max_matches < 1 or max_matches > 20:
            raise ValueError("max_matches must be between 1 and 20")
        if max_catalog_entries < 0:
            raise ValueError("max_catalog_entries must be >= 0")
        if max_sessions < 1:
            raise ValueError("max_sessions must be >= 1")
        seen: set[str] = set()
        searchable: list[ToolRegistration] = []
        for registration in registrations:
            if not isinstance(registration, ToolRegistration):
                raise TypeError("registrations must contain ToolRegistration instances")
            name = registration.definition.name
            if name in seen:
                raise ValueError(f"duplicate tool registration for {name!r}")
            seen.add(name)
            if name == tool_name:
                continue
            searchable.append(registration)
        self._registrations = tuple(searchable)
        self._candidate_prefixes = tuple(candidate_prefixes)
        self._tool_name = tool_name
        self._max_matches = max_matches
        self._max_catalog_entries = max_catalog_entries
        self._max_sessions = max_sessions
        self._candidates = frozenset(
            registration.definition.name
            for registration in self._registrations
            if self._is_candidate(registration.definition.name)
        )
        # Only candidates may ever be returned or activated. When this
        # capability is constructed with the full registration set, Forge and
        # basic tool schemas must not leak through the search result.
        self._searchable = tuple(
            registration
            for registration in self._registrations
            if registration.definition.name in self._candidates
        )
        self._activated: OrderedDict[str, frozenset[str]] = OrderedDict()
        self._definition = ToolDefinition(
            name=tool_name,
            description=(
                "Search on-demand tools by name or description. Returns the "
                "complete JSON schema of each match, and makes matches visible "
                "from the next model step. Discovery is not execution "
                "authorization; the active policy still decides."
            ),
            parameters=_SEARCH_PARAMETERS,
        )
        self._registration = ToolRegistration(
            definition=self._definition,
            handler=self._handle,
        )

    @property
    def name(self) -> str:
        return "tool_search"

    def tool_registrations(self) -> tuple[ToolRegistration, ...]:
        # With no candidate tools there is nothing to discover; keeping an
        # always-visible search tool would only spend prompt tokens.
        if not self._candidates:
            return ()
        return (self._registration,)

    @property
    def has_candidates(self) -> bool:
        return bool(self._candidates)

    def hidden_tools(self, context: ExecutionContext) -> frozenset[str]:
        """Candidate tools not yet discovered by this session.

        The session is touched here because prompt assembly asks this on every
        step; the most recently used sessions survive the bounded LRU.
        """
        activated = self._activated.get(context.session_id)
        if activated is not None:
            self._activated.move_to_end(context.session_id)
        return frozenset(self._candidates - (activated or frozenset()))

    def release_session(self, session_id: str) -> None:
        """Drop this session's discoveries so a reused id starts clean."""
        self._activated.pop(session_id, None)

    def prompt_context(self, context: ExecutionContext) -> str | None:
        if not self._candidates:
            return None
        hidden = self.hidden_tools(context)
        if not hidden and not self._activated.get(context.session_id):
            return (
                "Tool discovery: use tool_search(query) to load the full schema "
                "of an on-demand tool before calling it."
            )
        names = sorted(hidden)
        if not names:
            return (
                "Tool discovery: every on-demand tool is already discovered. "
                "Use tool_search(query) if you need to re-check a schema."
            )
        limit = self._max_catalog_entries
        shown = names[:limit]
        by_name = {registration.definition.name: registration for registration in self._searchable}
        lines = [
            "Tool discovery: call tool_search(query) to load a tool's full schema. "
            "On-demand tools:"
        ]
        for name in shown:
            description = by_name[name].definition.description.strip().splitlines()[0]
            if len(description) > 90:
                description = description[:87] + "..."
            lines.append(f"- {name}: {description}" if description else f"- {name}")
        if len(names) > len(shown):
            lines.append(f"- ... and {len(names) - len(shown)} more")
        return "\n".join(lines)

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None:
        """Forget this session's discoveries on cancellation.

        Losing them is safe: the tools become hidden again and must be
        searched again. Keeping stale discoveries across a reused session id
        would be the riskier choice.
        """
        del context
        self.release_session(session_id)

    async def aclose(self) -> None:
        self._activated.clear()

    async def _handle(
        self, call: ToolCall, context: ExecutionContext
    ) -> ToolOutcome:
        arguments = call.arguments_dict()
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return self._failed(
                call,
                "tool_search_invalid_query",
                "query must be a non-empty string",
            )
        limit = arguments.get("limit", self._max_matches)
        if not isinstance(limit, int) or isinstance(limit, bool):
            return self._failed(
                call, "tool_search_invalid_limit", "limit must be an integer"
            )
        limit = max(1, min(limit, 20))
        matches = self._search(query, limit)
        candidates = [item.definition.name for item in matches]
        self._activate(context.session_id, candidates)
        payload = [
            {
                "name": item.definition.name,
                "description": item.definition.description,
                "parameters": dict(item.definition.parameters),
            }
            for item in matches
        ]
        if not payload:
            return ToolOutcome(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                status=ToolStatus.SUCCEEDED,
                text=f"No tool matched {query!r}.",
                content={"matches": [], "activated": []},
            )
        text = (
            f"Discovered {len(payload)} tool(s) for {query!r}. They become "
            "visible from the next model step; the active policy still decides "
            "whether they may execute.\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        )
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=text,
            content={
                "matches": [item.definition.name for item in matches],
                "activated": sorted(
                    set(candidates) & set(self._candidates)
                ),
            },
        )

    def _search(self, query: str, limit: int) -> list[ToolRegistration]:
        text = query.strip().lower()
        terms = [term for term in text.replace("_", " ").split() if term]
        scored: list[tuple[int, str, ToolRegistration]] = []
        for registration in self._searchable:
            name = registration.definition.name
            haystack = f"{name} {registration.definition.description}".lower()
            score = 0
            if text == name.lower():
                score = 100
            elif text in name.lower():
                score = 60
            elif text in haystack:
                score = 40
            elif terms and all(term in haystack for term in terms):
                score = 20
            if score:
                scored.append((score, name, registration))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[:limit]]

    def _activate(self, session_id: str, names: Sequence[str]) -> None:
        candidate_hits = frozenset(name for name in names if name in self._candidates)
        if not candidate_hits:
            return
        current = self._activated.get(session_id, frozenset())
        self._activated[session_id] = current | candidate_hits
        self._activated.move_to_end(session_id)
        while len(self._activated) > self._max_sessions:
            self._activated.popitem(last=False)

    def _is_candidate(self, name: str) -> bool:
        if name == self._tool_name or name in _NEVER_CANDIDATE_NAMES:
            return False
        if any(name.startswith(prefix) for prefix in _NEVER_CANDIDATE_PREFIXES):
            return False
        return any(name.startswith(prefix) for prefix in self._candidate_prefixes)

    def _failed(self, call: ToolCall, code: str, message: str) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.FAILED,
            text=message,
            error=ToolError(code, message),
        )
