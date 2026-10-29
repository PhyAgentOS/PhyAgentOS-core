"""Memory system for persistent agent memory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from PhyAgentOS.utils.helpers import (
    ensure_dir,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
)

if TYPE_CHECKING:
    from PhyAgentOS.providers.base import LLMProvider
    from PhyAgentOS.providers.service import RuntimeSelection
    from PhyAgentOS.session.manager import Session, SessionManager


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


def _ensure_text(value: Any) -> str:
    """Normalize tool-call payload values to text for file storage."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None


_SESSION_SUMMARY_SYSTEM_PROMPT = """You compress the older messages of one chat session into a running summary. The summary replaces those messages in the live context, so preserve everything needed to continue the task.

Return a concise markdown summary with these sections:
- Current goal
- Constraints
- Unfinished work
- Key tools and results (reference tool calls, task/action identities, and result facts exactly as observed)
- Physical state: repeat that current physical state is unknown here and must be queried from Forge records

Never infer or restate current robot/action state as fact. Action, task, and binding identities are references only; their status must be queried from TaskStore/Forge records."""

_SESSION_SUMMARY_USER_PROMPT = """## Previous session summary
{previous}

## Messages to compress
{messages}"""

_MAX_SESSION_SUMMARY_CHARS = 8000
_SUMMARY_MESSAGE_MAX_CHARS = 2000

_WORKSPACE_LOCKS: dict[str, asyncio.Lock] = {}


def _workspace_lock(workspace: Path) -> asyncio.Lock:
    """Return the process-wide MEMORY.md lock for one resolved workspace."""
    key = str(Path(workspace).expanduser().resolve())
    lock = _WORKSPACE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _WORKSPACE_LOCKS[key] = lock
    return lock


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).expanduser().resolve()
        self.memory_dir = ensure_dir(self.workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        """Replace MEMORY.md atomically so a reader never sees a partial file."""
        fd, temp_path = tempfile.mkstemp(
            dir=self.memory_dir, prefix=".MEMORY.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(temp_path, self.memory_file)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}: {message['content']}"
            )
        return "\n".join(lines)

    async def consolidate(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
    ) -> bool:
        """Consolidate the provided message chunk into MEMORY.md + HISTORY.md."""
        if not messages:
            return True
        # MEMORY.md is a read-modify-write value shared by every store and
        # consolidator pointed at this workspace in the process.
        async with _workspace_lock(self.workspace):
            return await self._consolidate_locked(messages, provider, model)

    async def _consolidate_locked(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
    ) -> bool:
        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{self._format_messages(messages)}"""

        try:
            response = await provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=model,
                tool_choice="required",
                reasoning_effort=None,
            )

            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Memory consolidation: unexpected save_memory arguments")
                return False

            if entry := args.get("history_entry"):
                self.append_history(_ensure_text(entry))
            if update := args.get("memory_update"):
                update = _ensure_text(update)
                if update != current_memory:
                    self.write_long_term(update)

            logger.info("Memory consolidation done for {} messages", len(messages))
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False


class MemoryConsolidator:
    """Owns consolidation policy, locking, and session offset updates."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
    ):
        self.store = MemoryStore(workspace)
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def consolidate_messages(
        self,
        messages: list[dict[str, object]],
        runtime: RuntimeSelection | None = None,
    ) -> bool:
        """Archive a selected message chunk into persistent memory."""
        provider = runtime.provider if runtime is not None else self.provider
        model = runtime.model if runtime is not None else self.model
        return await self.store.consolidate(messages, provider, model)

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def estimate_session_prompt_tokens(
        self,
        session: Session,
        runtime: RuntimeSelection | None = None,
    ) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view."""
        history = session.get_history(max_messages=0)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            runtime.provider if runtime is not None else self.provider,
            runtime.model if runtime is not None else self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    @staticmethod
    def _format_session_messages(messages: list[dict[str, Any]]) -> str:
        """Render messages for the compression prompt, keeping tool references."""
        lines = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            text = str(content or "")
            if len(text) > _SUMMARY_MESSAGE_MAX_CHARS:
                text = text[:_SUMMARY_MESSAGE_MAX_CHARS] + " …[truncated]"
            refs = ""
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    refs += f" [tool_call: {call.get('name', '?')} id={call.get('id', '?')}]"
            if message.get("tool_call_id"):
                refs += f" [tool_call_id: {message['tool_call_id']}]"
            if message.get("name"):
                refs += f" [name: {message['name']}]"
            lines.append(f"{str(message.get('role', '?')).upper()}: {text}{refs}")
        return "\n".join(lines)

    async def _compress_session_locked(
        self,
        session: Session,
        end_idx: int,
        runtime: RuntimeSelection | None = None,
    ) -> bool:
        """Compress into the session summary; return False without advancing on failure."""
        start = session.last_consolidated
        if end_idx <= start:
            return True
        chunk = session.messages[start:end_idx]
        if not chunk:
            return True

        prompt = _SESSION_SUMMARY_USER_PROMPT.format(
            previous=session.summary or "(empty)",
            messages=self._format_session_messages(chunk),
        )
        try:
            response = await (
                runtime.provider if runtime is not None else self.provider
            ).chat_with_retry(
                messages=[
                    {"role": "system", "content": _SESSION_SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                model=runtime.model if runtime is not None else self.model,
                reasoning_effort=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Session compression failed for {}", session.key)
            return False

        summary = (response.content or "").strip()
        if getattr(response, "finish_reason", None) == "error" or not summary:
            logger.warning("Session compression: empty or failed summary for {}", session.key)
            return False
        if len(summary) > _MAX_SESSION_SUMMARY_CHARS:
            summary = summary[:_MAX_SESSION_SUMMARY_CHARS]

        previous_summary = session.summary
        previous_offset = session.last_consolidated
        session.summary = summary
        session.last_consolidated = end_idx
        try:
            self.sessions.save(session)
        except asyncio.CancelledError:
            session.summary = previous_summary
            session.last_consolidated = previous_offset
            raise
        except Exception:
            session.summary = previous_summary
            session.last_consolidated = previous_offset
            logger.exception(
                "Session compression: failed to persist summary for {}", session.key
            )
            return False
        return True

    def _archive_session_summary(self, session: Session, summary: str) -> bool:
        """Append the session summary once per session, keyed by session key + digest."""
        digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]
        marker = f"[session-summary:{session.key}:{digest}]"
        try:
            existing = self.store.history_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""
        if marker in existing:
            return True
        entry = (
            f"{datetime.now().strftime('[%Y-%m-%d %H:%M]')} {marker}\n"
            f"Session summary from compressed messages:\n{summary}"
        )
        try:
            self.store.append_history(entry)
        except Exception:
            logger.exception("Failed to archive session summary for {}", self.store.workspace)
            return False
        return True

    async def archive_unconsolidated(
        self,
        session: Session,
        runtime: RuntimeSelection | None = None,
    ) -> bool:
        """Archive the unconsolidated tail and the session summary for /new.

        The summary covers messages already compressed out of the tail, so it is
        written separately. Nothing is cleared here; the caller clears the
        session only after this returns True.
        """
        lock = self.get_lock(session.key)
        async with lock:
            start = session.last_consolidated
            tail = session.messages[start:]
            summary = session.summary.strip()

            if tail:
                if not await self.consolidate_messages(tail, runtime):
                    return False
                session.last_consolidated = len(session.messages)
                try:
                    self.sessions.save(session)
                except Exception:
                    session.last_consolidated = start
                    logger.exception(
                        "Failed to persist archived offset for {}", session.key
                    )
                    return False

            if summary:
                if not self._archive_session_summary(session, summary):
                    return False
                session.summary = ""
                try:
                    self.sessions.save(session)
                except Exception:
                    session.summary = summary
                    logger.exception(
                        "Failed to persist cleared summary for {}", session.key
                    )
                    return False
            return True

    async def maybe_consolidate_by_tokens(
        self,
        session: Session,
        runtime: RuntimeSelection | None = None,
    ) -> None:
        """Loop: compress old messages into the session summary until the prompt fits.

        This never touches MEMORY.md/HISTORY.md. Only a successful summary that
        was persisted advances ``last_consolidated``; a failed round leaves the
        session unchanged so the next turn can retry.
        """
        window = (
            (runtime.context_window_tokens or self.context_window_tokens)
            if runtime is not None
            else self.context_window_tokens
        )
        if not session.messages or window <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            provider = runtime.provider if runtime is not None else self.provider
            generation = getattr(provider, "generation", None)
            reserve = int(getattr(generation, "max_tokens", 0) or 0) or 4096
            target = max(1, (window - reserve) // 2)
            estimated, source = self.estimate_session_prompt_tokens(session, runtime)
            if estimated <= 0:
                return
            if estimated + reserve <= window:
                logger.debug(
                    "Token consolidation idle {}: {}+{}/{} via {}",
                    session.key,
                    estimated,
                    reserve,
                    window,
                    source,
                )
                return

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    return

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                end_idx = boundary[0]
                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    return

                logger.info(
                    "Session compression round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    window,
                    source,
                    len(chunk),
                )
                if not await self._compress_session_locked(session, end_idx, runtime):
                    return

                estimated, source = self.estimate_session_prompt_tokens(session, runtime)
                if estimated <= 0:
                    return
