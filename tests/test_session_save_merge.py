"""Saving a stale in-memory session must not drop messages another process appended."""

from __future__ import annotations

import json
from pathlib import Path

from PhyAgentOS.session.manager import SessionManager


def _messages(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [row for row in rows if row.get("_type") != "metadata"]


def test_save_merges_an_append_from_another_manager(tmp_path: Path) -> None:
    first = SessionManager(tmp_path)
    stale = first.get_or_create("cli:test")
    stale.messages.append({"role": "user", "content": "first"})
    first.save(stale)

    second = SessionManager(tmp_path)
    fresh = second.get_or_create("cli:test")
    fresh.messages.append({"role": "assistant", "content": "second"})
    second.save(fresh)

    # ``stale`` still holds only the first message. Saving it must keep the
    # assistant message written by the other manager.
    first.save(stale)

    path = first._get_session_path("cli:test")
    assert [m["content"] for m in _messages(path)] == ["first", "second"]


def test_replace_clears_disk_history(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:test")
    session.messages.append({"role": "user", "content": "old"})
    manager.save(session)

    session.clear()
    manager.save(session, replace=True)

    path = manager._get_session_path("cli:test")
    assert _messages(path) == []
