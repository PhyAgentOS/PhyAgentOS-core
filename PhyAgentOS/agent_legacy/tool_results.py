"""Bound the size of one tool result before it enters the model context."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

DEFAULT_MAX_CHARS = 8_000
OUTPUT_DIR = Path(".paos") / "tool_outputs"

CRITICAL_ORDER = ("status", "invocation_id", "attempt_id", "task_id", "session_id")
CRITICAL_KEYS = frozenset(CRITICAL_ORDER)
SUMMARY_ORDER = ("error", "remote_state", "evidence_refs")
SUMMARY_KEYS = frozenset(SUMMARY_ORDER)
RESULT_KEY = "result"
SUMMARY_MAX_CHARS = 1_000
FIELD_MAX_CHARS = 1_000
CRITICAL_MAX_CHARS = 128
OMITTED_KEYS_MAX = 10


class ToolResultLimiter:
    """Truncate oversized tool results while keeping safety-critical fields."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.max_chars = DEFAULT_MAX_CHARS

    def limit(self, tool_call_id: str, result: str) -> str:
        if not isinstance(result, str) or len(result) <= self.max_chars:
            return result

        data = self._parse(result)
        if isinstance(data, dict):
            compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if len(compact) <= self.max_chars:
                return compact

        path = self._write_original(tool_call_id, result)
        display = self._display_path(path)

        if isinstance(data, dict):
            return self._limit_json(data, result, display)

        return self._truncate(result, display, self.max_chars)

    def _parse(self, result: str) -> Any:
        try:
            return json.loads(result)
        except (TypeError, ValueError):
            return None

    def _limit_json(self, data: dict[str, Any], original: str, display: str | None) -> str:
        candidates = (
            (FIELD_MAX_CHARS, SUMMARY_MAX_CHARS, False),
            (200, 200, False),
            (200, SUMMARY_MAX_CHARS, True),
            (40, 200, True),
            (40, 80, True),
            (40, 40, True),
        )
        serialized = ""
        for field_max, summary_max, compact in candidates:
            limited = self._limit_value(
                data, display, field_max, summary_max, compact=compact
            )
            limited["_truncated"] = {
                "original_chars": len(original),
                "original_path": display,
            }
            serialized = self._serialize(limited)
            if len(serialized) <= self.max_chars:
                return serialized

        critical = self._critical_only(data, display)
        critical["_truncated"] = {
            "original_chars": len(original),
            "original_path": display,
        }
        serialized = self._serialize(critical)
        if len(serialized) <= self.max_chars:
            return serialized
        return self._truncate(serialized, display, self.max_chars)

    def _limit_value(
        self,
        value: Any,
        display: str | None,
        field_max: int,
        summary_max: int,
        *,
        compact: bool,
    ) -> Any:
        if isinstance(value, dict):
            limited: dict[str, Any] = {}
            omitted: list[str] = []
            for key, item in value.items():
                if key in CRITICAL_KEYS:
                    limited[key] = self._keep_critical(item, display)
                elif key in SUMMARY_KEYS:
                    limited[key] = (
                        self._summarize(item, display, summary_max)
                        if compact
                        else item
                    )
                elif key == RESULT_KEY:
                    limited[key] = self._summarize(item, display, summary_max)
                else:
                    child = self._limit_value(
                        item, display, field_max, summary_max, compact=compact
                    )
                    if compact and not self._has_content(child):
                        omitted.append(key)
                    else:
                        limited[key] = child
            if omitted:
                limited["_omitted_keys"] = omitted[:OMITTED_KEYS_MAX]
            return limited
        if isinstance(value, list):
            items = [
                self._limit_value(
                    item, display, field_max, summary_max, compact=compact
                )
                for item in value
            ]
            if compact:
                items = [item for item in items if self._has_content(item)]
            return items
        if isinstance(value, str) and len(value) > field_max:
            return self._truncate(value, display, field_max)
        return value

    def _keep_critical(self, value: Any, display: str | None) -> Any:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False)
        if len(text) <= CRITICAL_MAX_CHARS:
            return value
        return self._truncate(text, display, CRITICAL_MAX_CHARS)

    def _critical_only(self, value: Any, display: str | None) -> Any:
        if isinstance(value, dict):
            limited: dict[str, Any] = {}
            for key, item in value.items():
                if key in CRITICAL_KEYS:
                    limited[key] = self._keep_critical(item, display)
                elif key in SUMMARY_KEYS or key == RESULT_KEY:
                    limited[key] = self._summarize(item, display, 40)
                elif isinstance(item, dict):
                    child = self._critical_only(item, display)
                    if child:
                        limited[key] = child
                elif isinstance(item, list):
                    children = [
                        child
                        for child in (
                            self._critical_only(entry, display) for entry in item
                        )
                        if child
                    ]
                    if children:
                        limited[key] = children
            return limited
        return {}

    @staticmethod
    def _has_content(value: Any) -> bool:
        if isinstance(value, dict):
            return bool(value)
        if isinstance(value, list):
            return bool(value)
        return value is not None and value != ""

    @staticmethod
    def _serialize(limited: dict[str, Any]) -> str:
        ordered: dict[str, Any] = {}
        for key in CRITICAL_ORDER:
            if key in limited:
                ordered[key] = limited[key]
        for key in SUMMARY_ORDER:
            if key in limited:
                ordered[key] = limited[key]
        for key, value in limited.items():
            if key not in ordered:
                ordered[key] = value
        return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))

    def _summarize(self, value: Any, display: str | None, limit: int) -> Any:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False)
        if len(text) <= limit:
            return value
        return self._truncate(text, display, limit)

    def _truncate(self, text: str, display: str | None, limit: int) -> str:
        marker = self._marker(len(text), limit, display)
        if len(marker) >= limit:
            return marker
        room = limit - len(marker)
        head = room * 3 // 5
        tail = room - head
        suffix = text[-tail:] if tail else ""
        return text[:head] + marker + suffix

    @staticmethod
    def _marker(total: int, limit: int, display: str | None) -> str:
        if display is None:
            return f"\n...[truncated to {limit} of {total} chars; full output unavailable]...\n"
        return f"\n...[truncated to {limit} of {total} chars; full output: {display}]...\n"

    def _write_original(self, tool_call_id: str, result: str) -> Path | None:
        try:
            directory = self.workspace / OUTPUT_DIR
            directory.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha1(result.encode("utf-8")).hexdigest()[:8]
            path = directory / f"{self._safe_name(tool_call_id)}-{digest}.txt"
            path.write_text(result, encoding="utf-8")
            return path
        except Exception:
            return None

    def _display_path(self, path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return path.as_posix()

    @staticmethod
    def _safe_name(tool_call_id: str) -> str:
        safe = "".join(
            char if char.isalnum() or char in "._-" else "_"
            for char in str(tool_call_id)
        ).strip("._")
        return safe[:120] or "tool_result"
