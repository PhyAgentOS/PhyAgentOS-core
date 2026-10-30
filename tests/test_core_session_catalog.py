"""Tests for the CLI session catalog.

Covered here: daily id allocation, monotonic counters across restarts and
days, concurrent allocation through separate connections, discovery of
event-only sessions with their original ids, empty sessions, local title
derivation, and per-session selection metadata.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.session import SQLiteSessionStore
from PhyAgentOS.agent.services.session_catalog import (
    SessionCatalog,
    derive_title,
)


def _clock(value: datetime):
    return lambda: value


@pytest.fixture()
def catalog_path(tmp_path: Path) -> Path:
    directory = tmp_path / "sessions"
    directory.mkdir()
    return directory / "core.db"


def test_create_session_allocates_daily_ids_and_keeps_name_independent(
    catalog_path: Path,
) -> None:
    clock = _clock(datetime(2026, 10, 30, 9, 0))
    catalog = SessionCatalog(catalog_path, clock=clock)
    try:
        first = catalog.create_session()
        assert first.session_id == "cli:20261030-01"
        assert first.title is None
        assert first.label == "cli:20261030-01"
        assert first.last_sequence == 0

        name = catalog.name_from_first_input(
            first.session_id, "Fix the failing LIBERO evaluation"
        )
        assert name == "Fix the failing LIBERO evaluation"
        info = catalog.get_session(first.session_id)
        assert info is not None
        assert info.session_id == "cli:20261030-01"
        assert info.title == name
        assert info.label == f"{name} - cli:20261030-01"

        second = catalog.create_session()
        assert second.session_id == "cli:20261030-02"
        assert catalog.get_session(second.session_id) is not None
    finally:
        catalog.close()


def test_counter_survives_restart_and_never_reuses_numbers(catalog_path: Path) -> None:
    clock = _clock(datetime(2026, 10, 30, 9, 0))
    first = SessionCatalog(catalog_path, clock=clock)
    try:
        ids = [first.create_session().session_id for _ in range(2)]
    finally:
        first.close()
    assert ids == ["cli:20261030-01", "cli:20261030-02"]

    second = SessionCatalog(catalog_path, clock=clock)
    try:
        third = second.create_session()
        assert third.session_id == "cli:20261030-03"
        assert {info.session_id for info in second.list_sessions()} == {
            *ids,
            "cli:20261030-03",
        }
    finally:
        second.close()


def test_counter_starts_over_on_the_next_local_day(catalog_path: Path) -> None:
    now = [datetime(2026, 10, 30, 23, 59)]
    catalog = SessionCatalog(catalog_path, clock=lambda: now[0])
    try:
        assert catalog.create_session().session_id == "cli:20261030-01"
        assert catalog.create_session().session_id == "cli:20261030-02"
        now[0] = now[0] + timedelta(days=1)
        assert catalog.create_session().session_id == "cli:20261031-01"
        assert catalog.create_session().session_id == "cli:20261031-02"
    finally:
        catalog.close()

    reopened = SessionCatalog(catalog_path, clock=lambda: now[0])
    try:
        assert reopened.create_session().session_id == "cli:20261031-03"
    finally:
        reopened.close()


def test_separate_connections_allocate_unique_ids(catalog_path: Path) -> None:
    def worker(_: int) -> list[str]:
        catalog = SessionCatalog(catalog_path)
        try:
            return [catalog.create_session().session_id for _ in range(5)]
        finally:
            catalog.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(worker, range(4)))
    ids = [session_id for batch in batches for session_id in batch]

    assert len(ids) == 20
    assert len(set(ids)) == 20
    suffixes = sorted(int(session_id.rsplit("-", 1)[1]) for session_id in ids)
    assert suffixes == list(range(1, 21))

    catalog = SessionCatalog(catalog_path)
    try:
        assert len(catalog.list_sessions()) == 20
    finally:
        catalog.close()


def test_simultaneous_first_open_of_catalog_and_store_is_wal_safe(
    tmp_path: Path,
) -> None:
    """Eight connections race the first WAL switch, three fresh databases.

    The barrier starts every constructor at the same moment; passing does not
    depend on creation order or an external sleep.
    """
    workers = 8
    for round_index in range(3):
        db = tmp_path / f"core-{round_index}.db"
        barrier = threading.Barrier(workers)

        def catalog_worker(index: int) -> tuple[str, str]:
            barrier.wait(timeout=10)
            catalog = SessionCatalog(db)
            try:
                return "catalog", catalog.create_session().session_id
            finally:
                catalog.close()

        def store_worker(index: int) -> tuple[str, str]:
            barrier.wait(timeout=10)
            store = SQLiteSessionStore(db)
            session_id = f"event:{round_index}:{index}"
            try:
                asyncio.run(
                    store.append(
                        RuntimeEvent(
                            type=EventType.INPUT_ACCEPTED,
                            session_id=session_id,
                            payload={"characters": 1},
                        )
                    )
                )
                return "store", session_id
            finally:
                asyncio.run(store.aclose())

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(catalog_worker, index) if index % 2 == 0
                else pool.submit(store_worker, index)
                for index in range(workers)
            ]
            results = [future.result(timeout=60) for future in futures]

        assert len(results) == workers
        catalog_ids = [value for kind, value in results if kind == "catalog"]
        store_ids = [value for kind, value in results if kind == "store"]
        assert len(catalog_ids) == 4
        assert sorted(int(session_id.rsplit("-", 1)[1]) for session_id in catalog_ids) == [
            1,
            2,
            3,
            4,
        ]

        verifier = SQLiteSessionStore(db)
        try:
            for session_id in store_ids:
                assert len(asyncio.run(verifier.read_session(session_id))) == 1
        finally:
            asyncio.run(verifier.aclose())

        with sqlite3.connect(db) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert str(mode).lower() == "wal"


def test_non_busy_init_errors_are_not_retried(tmp_path: Path, monkeypatch) -> None:
    from PhyAgentOS.agent.core.session import _open_sqlite_connection

    sleeps: list[float] = []
    monkeypatch.setattr(
        "PhyAgentOS.agent.core.session.time.sleep", lambda delay: sleeps.append(delay)
    )
    with pytest.raises(sqlite3.OperationalError, match="syntax error"):
        _open_sqlite_connection(str(tmp_path / "bad.db"), 5000, "NOT VALID SQL;")
    assert sleeps == []


@pytest.mark.asyncio
async def test_event_only_sessions_are_listed_with_their_original_ids(
    catalog_path: Path,
) -> None:
    store = SQLiteSessionStore(catalog_path)
    legacy_id = "telegram:42"
    try:
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_STARTED, session_id=legacy_id, turn_id="turn-1"
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.REQUEST_PREPARED,
                session_id=legacy_id,
                turn_id="turn-1",
                step_id="step-1",
                payload={
                    "model_call_id": "m1",
                    "messages": [
                        {"role": "system", "content": "sys"},
                        {"role": "user", "content": "old prompt"},
                    ],
                    "tools": [],
                },
            )
        )
        await store.append(
            RuntimeEvent(
                type=EventType.TURN_COMPLETED,
                session_id=legacy_id,
                turn_id="turn-1",
                payload={"reply": "old answer"},
            )
        )
    finally:
        await store.aclose()

    catalog = SessionCatalog(catalog_path)
    try:
        listed = catalog.list_sessions()
        assert [info.session_id for info in listed] == [legacy_id]
        assert listed[0].title is None
        assert listed[0].last_sequence == 3

        ensured = catalog.ensure_session(legacy_id)
        assert ensured.session_id == legacy_id
        assert ensured.title is None
        assert [info.session_id for info in catalog.list_sessions()] == [legacy_id]
    finally:
        catalog.close()

    reader = SQLiteSessionStore(catalog_path)
    try:
        assert list(await reader.read_conversation(legacy_id)) == [
            {"role": "user", "content": "old prompt"},
            {"role": "assistant", "content": "old answer"},
        ]
    finally:
        await reader.aclose()


def test_empty_session_is_listed_with_a_clear_fallback_label(catalog_path: Path) -> None:
    catalog = SessionCatalog(catalog_path)
    try:
        created = catalog.create_session()
        assert catalog.get_session(created.session_id) == created
        assert [info.session_id for info in catalog.list_sessions()] == [created.session_id]
        assert created.label == created.session_id

        explicit = catalog.ensure_session("cli:manual")
        assert explicit.title is None
        assert explicit.last_sequence == 0
        assert explicit.label == "cli:manual"
        assert catalog.get_session("cli:unknown") is None
    finally:
        catalog.close()


def test_title_is_short_redacted_and_only_set_once(catalog_path: Path) -> None:
    catalog = SessionCatalog(catalog_path)
    try:
        session = catalog.create_session()
        long_prompt = (
            "Please fix the failing gateway integration. "
            "Use api_key=sk-abcdefgh12345678 and keep the change small."
        )
        title = catalog.name_from_first_input(session.session_id, long_prompt)
        assert title.startswith("Please fix the failing")
        assert title.endswith("…")
        assert "sk-abcdefgh12345678" not in title
        assert len(title) <= 37

        assert (
            catalog.name_from_first_input(session.session_id, "a completely new task")
            == title
        )

        blank = catalog.create_session()
        assert catalog.name_from_first_input(blank.session_id, "   ") == blank.session_id
        assert catalog.get_session(blank.session_id).title is None
    finally:
        catalog.close()


def test_derive_title_redacts_credentials_and_keeps_one_clause() -> None:
    assert derive_title("Run tests\nsecond line") == "Run tests"
    assert derive_title("Deploy with password=hunter2 now") == "Deploy with [redacted] now"
    assert derive_title("   ") is None
    assert derive_title("# Headline task.") == "Headline task"
    assert derive_title("\x1b[31mFix the build\x1b[0m\nnext") == "Fix the build"
    stored = derive_title("bell\x07 and \x7fdelete")
    assert stored is not None
    assert all(ord(character) >= 32 for character in stored)


@pytest.mark.asyncio
async def test_event_activity_updates_list_order(catalog_path: Path) -> None:
    catalog = SessionCatalog(
        catalog_path, clock=_clock(datetime(2020, 1, 1, 9, 0))
    )
    try:
        older = catalog.create_session()
        newer = catalog.create_session()
    finally:
        catalog.close()

    store = SQLiteSessionStore(catalog_path)
    try:
        await store.append(
            RuntimeEvent(
                type=EventType.INPUT_ACCEPTED,
                session_id=older.session_id,
                payload={"characters": 1},
                recorded_at=datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc),
            )
        )
    finally:
        await store.aclose()

    reopened = SessionCatalog(
        catalog_path, clock=_clock(datetime(2020, 1, 1, 9, 0))
    )
    try:
        listed = reopened.list_sessions()
        assert [info.session_id for info in listed] == [
            older.session_id,
            newer.session_id,
        ]
        assert listed[0].updated_at.year == 2030
        assert listed[0].selection == {}
    finally:
        reopened.close()


def test_selection_is_stored_per_session_and_survives_restart(
    catalog_path: Path,
) -> None:
    catalog = SessionCatalog(catalog_path)
    try:
        first = catalog.create_session()
        second = catalog.create_session()
        catalog.save_selection(
            first.session_id, provider="alpha", model="model-a", effort="high"
        )
        catalog.save_selection(second.session_id, provider="beta", model="model-b")

        assert catalog.load_selection(first.session_id).as_dict() == {
            "provider": "alpha",
            "model": "model-a",
            "effort": "high",
        }
        assert catalog.load_selection(second.session_id).as_dict() == {
            "provider": "beta",
            "model": "model-b",
        }
        assert catalog.load_selection("cli:missing") is None
    finally:
        catalog.close()

    reopened = SessionCatalog(catalog_path)
    try:
        selection = reopened.load_selection(first.session_id)
        assert selection is not None
        assert (selection.provider, selection.model, selection.effort) == (
            "alpha",
            "model-a",
            "high",
        )
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_catalog_and_store_share_one_database_file(catalog_path: Path) -> None:
    catalog = SessionCatalog(catalog_path)
    try:
        session = catalog.create_session()
    finally:
        catalog.close()

    store = SQLiteSessionStore(catalog_path)
    try:
        await store.append(
            RuntimeEvent(
                type=EventType.INPUT_ACCEPTED,
                session_id=session.session_id,
                payload={"characters": 4},
            )
        )
    finally:
        await store.aclose()

    with sqlite3.connect(catalog_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {"session_events", "session_catalog", "session_counters"} <= tables
    assert [path.name for path in catalog_path.parent.iterdir() if path.suffix == ".db"] == [
        "core.db"
    ]
