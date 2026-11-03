"""Task isolation: unresolved remote facts never become a workspace-wide gate."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

import PhyAgentOS.forge.task as task_module
from PhyAgentOS.agent.capabilities.forge_capability import ForgeCapability
from PhyAgentOS.agent.core.types import Budget, CancellationToken, ExecutionContext
from PhyAgentOS.config.schema import ForgeConfig
from PhyAgentOS.forge.task import (
    AgentTaskBusyError,
    AgentTaskCoordinator,
    AgentTaskError,
    AgentTaskRecord,
    AgentTaskStatus,
)
from PhyAgentOS.forge.tool_client import (
    ForgeToolAPIError,
    ForgeToolAPITimeoutError,
    ForgeToolClient,
)
from PhyAgentOS.verification.contracts import (
    TaskVerificationContract,
    VerificationEvidencePolicy,
)


def _contract() -> TaskVerificationContract:
    return TaskVerificationContract(
        mode="off",
        evidence_policy=VerificationEvidencePolicy(required_kinds=[]),
    )


def _seed_before(coordinator: AgentTaskCoordinator, task_id: str) -> None:
    """Skip evidence HTTP collection; the dispatch path is unchanged."""
    coordinator.store.update(
        task_id,
        lambda record: setattr(record, "before_snapshot_ref", "probe"),
        event_type="test_seeded_snapshot",
    )


def _seed_unknown_record(
    coordinator: AgentTaskCoordinator, task_id: str, updated_at: datetime
) -> None:
    """Append an Action record that is already unknown, without a dispatch."""
    from PhyAgentOS.forge.task import ToolExecutionRecord

    def seed(current: AgentTaskRecord) -> None:
        current.active_revision.execution_records.append(
            ToolExecutionRecord(
                record_id="tool_seeded",
                revision_id=current.active_revision_id,
                tool_id="motion.move_pose",
                semantics="action",
                caller_id=f"paos:{current.task_id}:{current.active_revision_id}:tool_seeded",
                status="unknown",
                unknown_deadline=updated_at + timedelta(seconds=60),
                updated_at=updated_at,
            )
        )

    coordinator.store.update(task_id, seed, event_type="test_seeded_unknown")


def _context(session_id: str) -> ExecutionContext:
    return ExecutionContext(
        session_id=session_id,
        turn_id="turn-1",
        step_id="step-1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


class _StatusClient:
    """Accept Actions, then report the configured phase for every GET."""

    def __init__(self, phase: str = "cancelled") -> None:
        self.phase = phase
        self.action_calls = 0
        self.status_calls: list[str] = []

    async def invoke_action(
        self, tool_id: str, arguments: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        self.action_calls += 1
        return {
            "ok": True,
            "data": {
                "tool_id": tool_id,
                "invocation_id": f"inv_{self.action_calls}",
                "attempt_id": f"attempt_{self.action_calls}",
            },
        }

    async def invocation_status(self, invocation_id: str) -> dict[str, Any]:
        self.status_calls.append(invocation_id)
        return {"ok": True, "data": {"invocation_id": invocation_id, "phase": self.phase}}


async def test_read_timeout_keeps_unknown_but_new_sessions_can_start(tmp_path) -> None:
    posts: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append((request.method, request.url.path))
        raise httpx.ReadTimeout("gateway did not reply", request=request)

    client = ForgeToolClient(
        "http://gateway.test",
        timeout_s=1.0,
        transport=httpx.MockTransport(handler),
    )
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path, config=ForgeConfig(), client=client
    )
    old = coordinator.create_task(
        task_description="old task",
        verification=_contract(),
        origin_session_key="session-a",
    )
    _seed_before(coordinator, old.task_id)
    capability = ForgeCapability(coordinator)
    try:
        with pytest.raises(ForgeToolAPITimeoutError):
            await coordinator.start_action(
                old.task_id, "motion.move_pose", {"distance_m": 0.05}
            )
        record = coordinator.get_task(old.task_id).execution_records[0]
        assert record.status == "unknown"
        assert record.invocation_id is None

        # The next request in the same session is not pointed back at the task
        # that cannot be advanced locally.
        same_session_prompt = capability.prompt_context(_context("session-a"))
        assert same_session_prompt is None
        assert old.task_id not in (same_session_prompt or "")

        # Reconcile does not resolve or re-dispatch a no-identity unknown.
        reconciled = await coordinator.reconcile_nonterminal()
        assert reconciled is not None
        assert coordinator.get_task(old.task_id).execution_records[0].status == "unknown"
        assert posts == [("POST", "/tools/motion.move_pose:invoke")]

        other = coordinator.create_task(
            task_description="new session task",
            verification=_contract(),
            origin_session_key="session-b",
        )
        assert other.task_id != old.task_id
        assert coordinator.get_task(old.task_id).status == AgentTaskStatus.EXECUTING

        newer = coordinator.create_task(
            task_description="same session new task",
            verification=_contract(),
            origin_session_key="session-a",
        )
        assert newer.task_id != old.task_id
        stale = coordinator.get_task(old.task_id)
        assert stale.status == AgentTaskStatus.FAILED
        assert stale.execution_records[0].status == "unknown"
        assert stale.execution_records[0].invocation_id is None
        assert any("superseded locally" in item for item in stale.evidence_errors)

        prompt = capability.prompt_context(_context("session-c"))
        assert prompt is None
        assert old.task_id not in (prompt or "")
        current_prompt = capability.prompt_context(_context("session-a"))
        assert current_prompt is not None
        assert newer.task_id in current_prompt
        assert old.task_id not in current_prompt
        assert posts == [("POST", "/tools/motion.move_pose:invoke")]
    finally:
        await client.close()


async def test_cancel_after_timeout_allows_same_session_new_task(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("gateway did not reply", request=request)

    client = ForgeToolClient(
        "http://gateway.test",
        timeout_s=1.0,
        transport=httpx.MockTransport(handler),
    )
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path, config=ForgeConfig(), client=client
    )
    old = coordinator.create_task(
        task_description="old task",
        verification=_contract(),
        origin_session_key="session-a",
    )
    _seed_before(coordinator, old.task_id)
    try:
        with pytest.raises(ForgeToolAPITimeoutError):
            await coordinator.start_action(
                old.task_id, "motion.move_pose", {"distance_m": 0.05}
            )
        cancelled = await coordinator.cancel_task(old.task_id, reason="user stop")
        assert cancelled.status == AgentTaskStatus.FAILED
        assert cancelled.execution_records[0].status == "unknown"

        created = coordinator.create_task(
            task_description="new task",
            verification=_contract(),
            origin_session_key="session-a",
        )
        assert created.task_id != old.task_id
        assert coordinator.get_task(old.task_id).execution_records[0].status == "unknown"
    finally:
        await client.close()


async def test_real_gateway_busy_is_still_rejected(tmp_path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            json={
                "ok": False,
                "error": {
                    "code": "FORGE_TOOL_GATEWAY_BUSY",
                    "message": "operation concurrency 1 is exhausted",
                    "retryable": True,
                },
            },
        )

    client = ForgeToolClient(
        "http://gateway.test",
        timeout_s=1.0,
        transport=httpx.MockTransport(handler),
    )
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path, config=ForgeConfig(), client=client
    )
    task = coordinator.create_task(
        task_description="busy",
        verification=_contract(),
        origin_session_key="session-a",
    )
    _seed_before(coordinator, task.task_id)
    try:
        with pytest.raises(ForgeToolAPIError) as caught:
            await coordinator.start_action(
                task.task_id, "motion.move_pose", {"distance_m": 0.05}
            )
        assert caught.value.status_code == 503
        assert caught.value.error_code == "FORGE_TOOL_GATEWAY_BUSY"
        record = coordinator.get_task(task.task_id).execution_records[0]
        assert record.status == "failed"
        assert record.rejected_before_dispatch is True
        assert calls == 1

        other = coordinator.create_task(
            task_description="another session",
            verification=_contract(),
            origin_session_key="session-b",
        )
        assert other.task_id != task.task_id
    finally:
        await client.close()


async def test_session_spec_mismatch_fails_before_the_post(tmp_path) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {
                    "tool_id": "motion.move_pose",
                    "semantics": "action",
                },
            },
        )

    client = ForgeToolClient(
        "http://gateway.test",
        timeout_s=1.0,
        transport=httpx.MockTransport(handler),
    )
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path, config=ForgeConfig(), client=client
    )
    task = coordinator.create_task(
        task_description="session mismatch",
        verification=_contract(),
        origin_session_key="session-a",
    )
    try:
        with pytest.raises(ForgeToolAPIError) as caught:
            await coordinator.start_session(task.task_id, "motion.move_pose", {})
        record = coordinator.get_task(task.task_id).execution_records[0]
        assert record.semantics == "session"
        assert record.status == "failed"
        assert record.rejected_before_dispatch is True
        assert caught.value.dispatched is False
    finally:
        await client.close()

    assert requests == [("GET", "/tools/motion.move_pose")]


async def test_connect_failure_does_not_trap_the_session_slot(tmp_path) -> None:
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        raise httpx.ConnectError("connection refused", request=request)

    client = ForgeToolClient(
        "http://gateway.test",
        timeout_s=1.0,
        transport=httpx.MockTransport(handler),
    )
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path, config=ForgeConfig(), client=client
    )
    old = coordinator.create_task(
        task_description="old task",
        verification=_contract(),
        origin_session_key="session-a",
    )
    _seed_before(coordinator, old.task_id)
    try:
        with pytest.raises(ForgeToolAPIError):
            await coordinator.start_action(
                old.task_id, "motion.move_pose", {"distance_m": 0.05}
            )
        record = coordinator.get_task(old.task_id).execution_records[0]
        assert record.status == "failed"
        assert record.rejected_before_dispatch is True

        same_session = coordinator.create_task(
            task_description="same session retry task",
            verification=_contract(),
            origin_session_key="session-a",
        )
        other_session = coordinator.create_task(
            task_description="other session task",
            verification=_contract(),
            origin_session_key="session-b",
        )
        stale = coordinator.get_task(old.task_id)
        assert stale.status == AgentTaskStatus.FAILED
        assert stale.execution_records[0].status == "failed"
        assert stale.execution_records[0].rejected_before_dispatch is True
        assert same_session.task_id != old.task_id
        assert other_session.task_id != old.task_id
    finally:
        await client.close()

    assert posts == 1


def test_concurrent_session_creates_are_atomic(tmp_path) -> None:
    def coordinator() -> AgentTaskCoordinator:
        return AgentTaskCoordinator(
            workspace=tmp_path, config=ForgeConfig(), client=object()  # type: ignore[arg-type]
        )

    def create(instance: AgentTaskCoordinator, session: str):
        return instance.create_task(
            task_description=session,
            verification=_contract(),
            origin_session_key=session,
        )

    first, second = coordinator(), coordinator()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result()
            for future in [
                pool.submit(create, first, "session-a"),
                pool.submit(create, second, "session-b"),
            ]
        ]
    assert {result.origin_session_key for result in results} == {
        "session-a",
        "session-b",
    }
    assert len(first.store.active_tasks()) == 2

    third, fourth = coordinator(), coordinator()
    barrier = threading.Barrier(2)

    def competing_create(instance: AgentTaskCoordinator):
        barrier.wait()
        return create(instance, "session-same")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(competing_create, third),
            pool.submit(competing_create, fourth),
        ]
        outcomes = [future.exception() or future.result() for future in futures]

    assert sum(isinstance(item, AgentTaskBusyError) for item in outcomes) == 1
    assert sum(item is not None and not isinstance(item, BaseException) for item in outcomes) == 1


async def test_reconcile_covers_every_active_task_and_skips_terminal_history(
    tmp_path,
) -> None:
    client = _StatusClient()
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path, config=ForgeConfig(), client=client
    )
    tasks = []
    for session in ("session-a", "session-b"):
        task = coordinator.create_task(
            task_description=session,
            verification=_contract(),
            origin_session_key=session,
        )
        _seed_before(coordinator, task.task_id)
        accepted = await coordinator.start_action(
            task.task_id, "motion.move_pose", {"distance_m": 0.05}
        )
        invocation_id = accepted["data"]["invocation_id"]
        coordinator.observe_action(
            task.task_id,
            invocation_id,
            {"ok": True, "data": {"phase": "unknown"}},
        )
        tasks.append((task.task_id, invocation_id))

    history = coordinator.create_task(
        task_description="terminal history",
        verification=_contract(),
        origin_session_key="session-old",
    )
    _seed_before(coordinator, history.task_id)
    history_action = await coordinator.start_action(
        history.task_id, "motion.move_pose", {"distance_m": 0.05}
    )
    history_invocation = history_action["data"]["invocation_id"]
    coordinator.observe_action(
        history.task_id,
        history_invocation,
        {"ok": True, "data": {"phase": "unknown"}},
    )
    coordinator.store.update(
        history.task_id,
        lambda current: setattr(current, "status", AgentTaskStatus.CANCELLED),
        event_type="test_mark_terminal",
    )

    reconciled = await coordinator.reconcile_nonterminal()

    assert reconciled is not None
    for task_id, invocation_id in tasks:
        record = coordinator.get_task(task_id).execution_records[0]
        assert record.invocation_id == invocation_id
        assert record.status == "cancelled"
    assert history_invocation not in client.status_calls
    assert sorted(client.status_calls) == sorted(
        invocation_id for _, invocation_id in tasks
    )
    history_record = coordinator.get_task(history.task_id).execution_records[0]
    assert history_record.status == "unknown"
    assert client.action_calls == 3


async def test_same_task_unknown_action_is_never_resent(tmp_path) -> None:
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        raise httpx.ReadTimeout("gateway did not reply", request=request)

    client = ForgeToolClient(
        "http://gateway.test",
        timeout_s=1.0,
        transport=httpx.MockTransport(handler),
    )
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path, config=ForgeConfig(), client=client
    )
    task = coordinator.create_task(task_description="move", verification=_contract())
    _seed_before(coordinator, task.task_id)
    try:
        with pytest.raises(ForgeToolAPITimeoutError):
            await coordinator.start_action(
                task.task_id, "motion.move_pose", {"distance_m": 0.05}
            )
        with pytest.raises(AgentTaskError, match="identical Action"):
            await coordinator.start_action(
                task.task_id, "motion.move_pose", {"distance_m": 0.05}
            )
    finally:
        await client.close()

    assert posts == ["/tools/motion.move_pose:invoke"]


def _read_timeout_client(posts: list[tuple[str, str]]) -> ForgeToolClient:
    def handler(request: httpx.Request) -> httpx.Response:
        posts.append((request.method, request.url.path))
        raise httpx.ReadTimeout("gateway did not reply", request=request)

    return ForgeToolClient(
        "http://gateway.test", timeout_s=1.0, transport=httpx.MockTransport(handler)
    )


async def test_unknown_deadline_fails_locally_and_releases_the_session(
    tmp_path, monkeypatch
) -> None:
    clock = [datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(task_module, "utc_now", lambda: clock[0])
    posts: list[tuple[str, str]] = []
    client = _read_timeout_client(posts)
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path,
        config=ForgeConfig(unknown_recovery_timeout_s=60),
        client=client,
    )
    old = coordinator.create_task(
        task_description="old work",
        verification=_contract(),
        origin_session_key="session-a",
    )
    _seed_before(coordinator, old.task_id)
    try:
        with pytest.raises(ForgeToolAPITimeoutError):
            await coordinator.start_action(
                old.task_id, "motion.move_pose", {"distance_m": 0.05}
            )
        record = coordinator.get_task(old.task_id).execution_records[0]
        assert record.status == "unknown"
        assert record.unknown_deadline == clock[0] + timedelta(seconds=60)

        clock[0] += timedelta(seconds=59)
        assert coordinator.expire_timed_out_unknowns() == []
        assert coordinator.get_task(old.task_id).execution_records[0].status == "unknown"

        clock[0] += timedelta(seconds=2)
        assert coordinator.expire_timed_out_unknowns() == [record.record_id]
        task = coordinator.get_task(old.task_id)
        expired = task.execution_records[0]
        assert task.status is AgentTaskStatus.FAILED
        assert expired.status == "failed"
        assert expired.recovery_expired is True
        assert expired.error is not None and expired.error["type"] == "RecoveryTimeout"
        assert coordinator.unknown_remote_blockers() == []

        new_task = coordinator.create_task(
            task_description="new work",
            verification=_contract(),
            origin_session_key="session-a",
        )
        assert new_task.task_id != old.task_id
    finally:
        await client.close()

    # The expired record is never re-sent: one POST for the whole scenario.
    assert posts == [("POST", "/tools/motion.move_pose:invoke")]


async def test_unknown_deadline_is_not_extended_by_reconcile(
    tmp_path, monkeypatch
) -> None:
    clock = [datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(task_module, "utc_now", lambda: clock[0])
    client = _StatusClient(phase="unknown")
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path,
        config=ForgeConfig(unknown_recovery_timeout_s=60),
        client=client,  # type: ignore[arg-type]
    )
    task = coordinator.create_task(
        task_description="move",
        verification=_contract(),
        origin_session_key="session-a",
    )
    _seed_before(coordinator, task.task_id)
    accepted = await coordinator.start_action(
        task.task_id, "motion.move_pose", {"distance_m": 0.05}
    )
    invocation_id = accepted["data"]["invocation_id"]
    coordinator.observe_action(
        task.task_id, invocation_id, {"ok": True, "data": {"phase": "unknown"}}
    )
    deadline = coordinator.get_task(task.task_id).execution_records[0].unknown_deadline
    assert deadline == clock[0] + timedelta(seconds=60)

    clock[0] += timedelta(seconds=59)
    await coordinator.reconcile_nonterminal()
    record = coordinator.get_task(task.task_id).execution_records[0]
    assert record.status == "unknown"
    assert record.unknown_deadline == deadline
    assert client.status_calls == [invocation_id]

    clock[0] += timedelta(seconds=2)
    await coordinator.reconcile_nonterminal()
    task = coordinator.get_task(task.task_id)
    record = task.execution_records[0]
    assert record.status == "failed"
    assert record.recovery_expired is True
    assert task.status is AgentTaskStatus.FAILED
    # Recovery still asks the Gateway once before the deadline settles it.
    assert client.status_calls == [invocation_id, invocation_id]
    assert client.action_calls == 1


async def test_legacy_unknown_expires_from_its_own_timestamp(tmp_path, monkeypatch) -> None:
    clock = [datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(task_module, "utc_now", lambda: clock[0])
    posts: list[tuple[str, str]] = []
    client = _read_timeout_client(posts)
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path,
        config=ForgeConfig(unknown_recovery_timeout_s=60),
        client=client,
    )
    task = coordinator.create_task(
        task_description="legacy unknown",
        verification=_contract(),
        origin_session_key="session-a",
    )
    _seed_before(coordinator, task.task_id)
    try:
        with pytest.raises(ForgeToolAPITimeoutError):
            await coordinator.start_action(
                task.task_id, "motion.move_pose", {"distance_m": 0.05}
            )
        old_time = clock[0] - timedelta(hours=2)

        def forget_deadline(current: AgentTaskRecord) -> None:
            record = current.execution_records[0]
            record.unknown_deadline = None
            record.updated_at = old_time

        coordinator.store.update(
            task.task_id, forget_deadline, event_type="test_legacy_unknown"
        )
        await coordinator.reconcile_nonterminal()
        record = coordinator.get_task(task.task_id).execution_records[0]
        assert record.recovery_expired is True
        assert record.unknown_deadline == old_time + timedelta(seconds=60)
    finally:
        await client.close()


async def test_managed_task_expiry_does_not_raise_or_hold_the_slot(
    tmp_path, monkeypatch
) -> None:
    clock = [datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(task_module, "utc_now", lambda: clock[0])
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path,
        config=ForgeConfig(unknown_recovery_timeout_s=60),
        client=object(),  # type: ignore[arg-type]
    )
    task = coordinator.create_task(
        task_description="managed unknown",
        verification=_contract(),
        origin_session_key="session-a",
    )
    _seed_unknown_record(coordinator, task.task_id, clock[0] - timedelta(seconds=61))
    monkeypatch.setattr(coordinator.interaction, "managed", lambda _task_id: True)

    assert coordinator.expire_timed_out_unknowns() == ["tool_seeded"]
    closed = coordinator.get_task(task.task_id)
    assert closed.status is AgentTaskStatus.FAILED
    assert closed.execution_records[0].recovery_expired is True


def test_expiry_skips_terminal_history(tmp_path, monkeypatch) -> None:
    clock = [datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(task_module, "utc_now", lambda: clock[0])
    coordinator = AgentTaskCoordinator(
        workspace=tmp_path, config=ForgeConfig(unknown_recovery_timeout_s=60),
        client=object(),  # type: ignore[arg-type]
    )
    task = coordinator.create_task(
        task_description="history",
        verification=_contract(),
        origin_session_key="session-old",
    )

    _seed_unknown_record(coordinator, task.task_id, clock[0] - timedelta(hours=2))

    def mark_history(current: AgentTaskRecord) -> None:
        current.status = AgentTaskStatus.CANCELLED
        current.terminal_at = clock[0]
        current.updated_at = clock[0]

    coordinator.store.update(task.task_id, mark_history, event_type="test_history")
    assert coordinator.expire_timed_out_unknowns() == []
    record = coordinator.get_task(task.task_id).execution_records[0]
    assert record.status == "unknown"
    assert record.recovery_expired is False
