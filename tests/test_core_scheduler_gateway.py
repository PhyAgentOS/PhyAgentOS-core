"""The gateway wires channels, cron and heartbeat onto one Core runtime.

These tests run the real gateway service classes: the Core session comes from
the CLI's ``open_core_session``, the cron service is the production
``CronService`` on a temporary store, and the heartbeat service performs its
real decision call. Only the provider, the Forge coordinator and the channel
transports are fakes.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from core_background_support import (
    FakeChannel,
    FakeChannelManager,
    FakeCoordinator,
    ScriptedProvider,
    fake_forge_builder,
    make_config,
    make_selection,
    publish_inbound,
    response,
    tool_call,
    wait_for,
)

from PhyAgentOS.agent.capabilities.cron_capability import CronCapability
from PhyAgentOS.agent.capabilities.message_capability import MessageCapability
from PhyAgentOS.agent.services.background_runtime import (
    TurnBinding,
    TurnRoutes,
    open_background_core,
)
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.cli.command_groups import agent_commands
from PhyAgentOS.cli.command_groups.gateway_commands import CoreGateway, run_core_gateway
from PhyAgentOS.config.schema import Config
from PhyAgentOS.cron import service as cron_service_module
from PhyAgentOS.cron.service import CronService
from PhyAgentOS.cron.types import CronSchedule
from PhyAgentOS.heartbeat.service import HeartbeatService
from PhyAgentOS.providers.service import ProviderService

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]


def _is_heartbeat_decision(tools) -> bool:
    return any(
        (tool.get("function") or {}).get("name") == "heartbeat"
        for tool in (tools or [])
    )


async def _open_gateway(tmp_path, monkeypatch, provider, *, config=None, heartbeat_provider=None):
    config = config or make_config(tmp_path)
    try:
        config.workspace_path.mkdir(parents=True, exist_ok=True)
    except TypeError:  # pragma: no cover - already a Path
        pass
    coordinator = FakeCoordinator()
    monkeypatch.setattr(
        agent_commands, "build_forge_components", fake_forge_builder(coordinator)
    )
    bus = MessageBus()
    telegram = FakeChannel("telegram")
    channels = FakeChannelManager({"telegram": telegram}, bus)
    cron = CronService(tmp_path / "cron" / "jobs.json")
    routes = TurnRoutes()

    async def send(message):
        if channels.get_channel(message.channel) is None:
            raise ValueError(f"unknown channel '{message.channel}'; it is not enabled")
        await bus.publish_outbound(message)

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=heartbeat_provider or provider,
        model="fake-model",
        interval_s=config.gateway.heartbeat.interval_s,
        enabled=False,
    )
    runtime_cm = open_background_core(
        config,
        service=ProviderService(config),
        selected=make_selection(provider),
        capabilities=(
            CronCapability(cron, routes),
            MessageCapability(routes, send),
        ),
        routes=routes,
        send=send,
    )
    return config, bus, telegram, channels, cron, heartbeat, runtime_cm, coordinator


async def _catalog_session_ids(runtime) -> list[str]:
    return [
        info.session_id
        for info in await asyncio.to_thread(runtime.session.catalog.list_sessions)
    ]


def _gateway(runtime, bus, channels, cron, heartbeat, workspace=None) -> CoreGateway:
    return CoreGateway(
        runtime=runtime,
        bus=bus,
        channels=channels,
        cron=cron,
        heartbeat=heartbeat,
        workspace=workspace,
    )


async def test_image_tool_delivers_path_and_thread_through_gateway_bus(
    tmp_path, monkeypatch,
) -> None:
    async def responder(index, messages, tools):
        if index == 1:
            return tool_call(
                "image-call", "image",
                {"mode": "display", "image_path": "frame.png", "text": "camera"},
            )
        assert "Image displayed successfully" in messages[-1]["content"]
        return response("image ready")

    opened = await _open_gateway(tmp_path, monkeypatch, ScriptedProvider(responder))
    config, bus, telegram, channels, cron, heartbeat, runtime_cm, _ = opened
    frame = config.workspace_path / "frame.png"
    frame.write_bytes(b"local-image-payload")
    async with runtime_cm as runtime:
        gateway = _gateway(runtime, bus, channels, cron, heartbeat)
        serve = asyncio.create_task(gateway.serve())
        try:
            await wait_for(lambda: channels.started)
            await publish_inbound(
                bus, "telegram", "42", "show image",
                metadata={"message_id": 5, "message_thread_id": 7, "reply_to": "5"},
            )
            await wait_for(lambda: len(telegram.sent) == 2)
            image, final = telegram.sent
            assert image.media == [str(frame)]
            assert image.reply_to == "5"
            assert image.metadata["message_thread_id"] == 7
            assert final.content == "image ready"
            assert final.metadata["_turn_final"] is True
        finally:
            await gateway.aclose()
            serve.cancel()
            await asyncio.gather(serve, return_exceptions=True)


async def test_thread_history_is_kept_and_other_sessions_are_isolated(
    tmp_path, monkeypatch
) -> None:
    async def responder(index, messages, tools):
        return response(f"reply-{index}")

    provider = ScriptedProvider(responder)
    (
        config,
        bus,
        telegram,
        channels,
        cron,
        heartbeat,
        runtime_cm,
        _,
    ) = await _open_gateway(tmp_path, monkeypatch, provider)

    async with runtime_cm as runtime:
        gateway = _gateway(runtime, bus, channels, cron, heartbeat)
        serve = asyncio.create_task(gateway.serve())
        await wait_for(lambda: channels.started)

        await publish_inbound(
            bus,
            "telegram",
            "42",
            "first thread message",
            metadata={"message_id": 5, "message_thread_id": 7},
            session_key="telegram:42:topic:7",
        )
        await wait_for(lambda: len(telegram.sent) == 1)

        await publish_inbound(
            bus,
            "telegram",
            "42",
            "second thread message",
            metadata={"message_id": 6, "message_thread_id": 7},
            session_key="telegram:42:topic:7",
        )
        await wait_for(lambda: len(telegram.sent) == 2)

        await publish_inbound(
            bus,
            "telegram",
            "42",
            "another topic",
            metadata={"message_id": 7, "message_thread_id": 9},
            session_key="telegram:42:topic:9",
        )
        await wait_for(lambda: len(telegram.sent) == 3)

        await gateway.aclose()
        serve.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve

        assert [m.content for m in telegram.sent] == [
            "reply-1",
            "reply-2",
            "reply-3",
        ]
        # One final reply per turn, thread metadata preserved.
        assert all(message.metadata["_turn_final"] for message in telegram.sent)
        assert [message.metadata["message_thread_id"] for message in telegram.sent] == [
            7,
            7,
            9,
        ]

        second_turn = "\n".join(
            str(message.get("content") or "")
            for message in provider.calls[1]["messages"]
        )
        third_turn = "\n".join(
            str(message.get("content") or "")
            for message in provider.calls[2]["messages"]
        )
        assert "first thread message" in second_turn
        assert "reply-1" in second_turn
        assert "first thread message" not in third_turn
        assert "reply-1" not in third_turn

        # Channel sessions keep their channel/chat/thread keys in the shared
        # Core database; the CLI default session is not reused.
        session_ids = await _catalog_session_ids(runtime)
        assert "telegram:42:topic:7" in session_ids
        assert "telegram:42:topic:9" in session_ids
        assert "cli:direct" not in session_ids


async def test_message_tool_answer_is_delivered_once_by_the_channel_path(
    tmp_path, monkeypatch
) -> None:
    async def responder(index, messages, tools):
        if index == 1:
            return tool_call("m1", "message", {"content": "sent by tool"})
        return response("duplicate final")

    provider = ScriptedProvider(responder)
    (
        config,
        bus,
        telegram,
        channels,
        cron,
        heartbeat,
        runtime_cm,
        _,
    ) = await _open_gateway(tmp_path, monkeypatch, provider)

    async with runtime_cm as runtime:
        gateway = _gateway(runtime, bus, channels, cron, heartbeat)
        serve = asyncio.create_task(gateway.serve())
        await wait_for(lambda: channels.started)
        await publish_inbound(
            bus, "telegram", "42", "hello", metadata={"message_thread_id": 7}
        )
        await wait_for(lambda: telegram.sent)
        await asyncio.sleep(0.05)
        await gateway.aclose()
        serve.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve

    assert [message.content for message in telegram.sent] == ["sent by tool"]


async def test_heartbeat_skip_does_not_open_a_turn(tmp_path, monkeypatch) -> None:
    async def responder(index, messages, tools):
        return tool_call("h1", "heartbeat", {"action": "skip"})

    provider = ScriptedProvider(responder)
    (
        config,
        bus,
        telegram,
        channels,
        cron,
        heartbeat,
        runtime_cm,
        _,
    ) = await _open_gateway(
        tmp_path, monkeypatch, provider, heartbeat_provider=provider
    )
    config.workspace_path.mkdir(parents=True, exist_ok=True)
    (config.workspace_path / "HEARTBEAT.md").write_text(
        "# Tasks\n- water the plants\n", encoding="utf-8"
    )

    async with runtime_cm as runtime:
        gateway = _gateway(runtime, bus, channels, cron, heartbeat)
        manager = asyncio.create_task(channels.start_all())
        await heartbeat._tick()
        await gateway.aclose()
        manager.cancel()
        with pytest.raises(asyncio.CancelledError):
            await manager

    assert telegram.sent == []
    assert len(provider.calls) == 1  # decision only, no Core turn
    assert provider.calls[0]["tools"][0]["function"]["name"] == "heartbeat"


async def test_heartbeat_target_is_frozen_before_the_turn(tmp_path, monkeypatch) -> None:
    holder: dict[str, CoreGateway] = {}

    async def responder(index, messages, tools):
        if _is_heartbeat_decision(tools):
            return tool_call(
                "h1", "heartbeat", {"action": "run", "tasks": "water the plants"}
            )
        # A different chat becomes the most recent one while the heartbeat
        # turn is already running.
        holder["gateway"].recent_chats.record(
            TurnBinding(channel="telegram", chat_id="999")
        )
        return response("plants are watered")

    provider = ScriptedProvider(responder)
    (
        config,
        bus,
        telegram,
        channels,
        cron,
        heartbeat,
        runtime_cm,
        _,
    ) = await _open_gateway(
        tmp_path, monkeypatch, provider, heartbeat_provider=provider
    )
    config.workspace_path.mkdir(parents=True, exist_ok=True)
    (config.workspace_path / "HEARTBEAT.md").write_text(
        "# Tasks\n- water the plants\n", encoding="utf-8"
    )

    async with runtime_cm as runtime:
        gateway = _gateway(
            runtime, bus, channels, cron, heartbeat, workspace=config.workspace_path
        )
        holder["gateway"] = gateway
        gateway.recent_chats.record(
            TurnBinding(
                channel="telegram", chat_id="42", metadata={"message_thread_id": 7}
            )
        )
        manager = asyncio.create_task(channels.start_all())
        await heartbeat._tick()
        await wait_for(lambda: telegram.sent)
        await gateway.aclose()
        manager.cancel()
        with pytest.raises(asyncio.CancelledError):
            await manager

    assert len(provider.calls) == 2  # decision + Core turn
    # The decision uses the configured selection, not a second model choice.
    assert provider.calls[0]["kwargs"]["model"] == "fake-model"
    assert [message.chat_id for message in telegram.sent] == ["42"]
    assert telegram.sent[0].content == "plants are watered"
    # The frozen thread metadata is kept on the heartbeat reply even though
    # another chat became the most recent one during the turn.
    assert telegram.sent[0].metadata["message_thread_id"] == 7
    assert telegram.sent[0].metadata["_turn_final"] is True


async def test_manual_trigger_cannot_interleave_with_a_tick(
    tmp_path, monkeypatch
) -> None:
    """The whole heartbeat cycle is serialized against ``trigger_now``.

    Without the service lock, a manual trigger could replace the target frozen
    for the scheduled tick between its execution and its notification, and the
    answer would go to the wrong chat.
    """
    order: list[str] = []
    execute_entered = asyncio.Event()
    release_tick = asyncio.Event()

    class OrderingProvider:
        async def chat_with_retry(self, *, messages, tools=None, **kwargs):
            if _is_heartbeat_decision(tools):
                order.append("decision")
                return tool_call(
                    f"h{len(order)}",
                    "heartbeat",
                    {"action": "run", "tasks": "do the thing"},
                )
            order.append("execute")
            execute_entered.set()
            await release_tick.wait()
            return response("tick answer")

    provider = OrderingProvider()
    (
        config,
        bus,
        telegram,
        channels,
        cron,
        heartbeat,
        runtime_cm,
        _,
    ) = await _open_gateway(
        tmp_path, monkeypatch, provider, heartbeat_provider=provider
    )
    config.workspace_path.mkdir(parents=True, exist_ok=True)
    (config.workspace_path / "HEARTBEAT.md").write_text(
        "# Tasks\n- do the thing\n", encoding="utf-8"
    )

    async with runtime_cm as runtime:
        gateway = _gateway(
            runtime, bus, channels, cron, heartbeat, workspace=config.workspace_path
        )
        gateway.recent_chats.record(
            TurnBinding(
                channel="telegram", chat_id="42", metadata={"message_thread_id": 7}
            )
        )
        sent_notifications: list[str] = []
        original_notify = gateway._on_heartbeat_notify

        async def record_notify(response):
            order.append("notify")
            sent_notifications.append(response)
            await original_notify(response)

        heartbeat.on_notify = record_notify
        manager = asyncio.create_task(channels.start_all())

        tick = asyncio.create_task(heartbeat._tick())
        await asyncio.wait_for(execute_entered.wait(), 2)

        # A different chat appears while the tick is inside its turn.
        gateway.recent_chats.record(TurnBinding(channel="telegram", chat_id="999"))
        trigger = asyncio.create_task(heartbeat.trigger_now())
        await asyncio.sleep(0.05)
        # The manual cycle is still waiting behind the scheduled tick.
        assert order == ["decision", "execute"]

        release_tick.set()
        await asyncio.wait_for(tick, 2)
        await asyncio.wait_for(trigger, 2)

        await gateway.aclose()
        manager.cancel()
        with pytest.raises(asyncio.CancelledError):
            await manager

    assert order == ["decision", "execute", "notify", "decision", "execute"]
    assert sent_notifications == ["tick answer"]
    assert [message.chat_id for message in telegram.sent] == ["42"]
    assert telegram.sent[0].metadata["message_thread_id"] == 7


class _FakeCronClock:
    """Deterministic store/timer time for the cron service.

    ``sleep`` never waits in real time: it records the requested delay, blocks
    until the test wakes it, then advances the fake clock by that delay, so a
    poll cycle stays observable and repeatable.
    """

    def __init__(self, start_ms: int = 1_000_000) -> None:
        self.now_ms = start_ms
        self.delays: list[float] = []
        self._gate = asyncio.Event()

    def now(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms

    async def sleep(self, delay: float) -> None:
        self.delays.append(round(delay, 6))
        await self._gate.wait()
        self._gate.clear()
        # Time passes only when the test lets the wait complete, so a job an
        # external process writes while the service is waiting is added at the
        # clock the service will next observe.
        self.advance(int(round(delay * 1000)))

    def wake(self) -> None:
        self._gate.set()


class _CronAsyncio:
    """Local ``asyncio`` shim: only ``sleep`` is controlled by the clock."""

    def __init__(self, real, clock: _FakeCronClock) -> None:
        self._real = real
        self._clock = clock

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def sleep(self, delay: float) -> None:
        await self._clock.sleep(delay)


def _install_cron_clock(monkeypatch, clock: _FakeCronClock) -> None:
    monkeypatch.setattr(cron_service_module, "_now_ms", clock.now)
    monkeypatch.setattr(
        cron_service_module, "asyncio", _CronAsyncio(asyncio, clock)
    )


async def test_a_running_service_discovers_jobs_added_by_another_process(
    tmp_path, monkeypatch
) -> None:
    """A gateway picks up CLI-created jobs on the same store without a restart."""
    clock = _FakeCronClock()
    _install_cron_clock(monkeypatch, clock)
    store_path = tmp_path / "cron" / "jobs.json"

    dispatched: list[str] = []

    async def on_job(job):
        dispatched.append(job.id)

    gateway = CronService(store_path)
    gateway.on_job = on_job
    await gateway.start()
    # An empty store still arms a bounded reload check.
    await wait_for(lambda: bool(clock.delays))
    assert clock.delays[-1] == CronService.RELOAD_CHECK_S

    # A second process (the CLI) writes jobs to the same file and never starts
    # a scheduler of its own.
    cli = CronService(store_path)
    later = cli.add_job(
        name="later",
        schedule=CronSchedule(kind="at", at_ms=clock.now() + 5000),
        message="later",
    )
    assert cli._timer_task is None
    assert cli.status()["enabled"] is False

    bytes_after_add = store_path.read_bytes()

    # The running service only had a 1s wake armed; it reloads on that wake.
    clock.wake()
    await wait_for(lambda: len(clock.delays) == 2)
    assert [job.id for job in gateway.list_jobs()] == [later.id]
    assert dispatched == []
    # A reload-only poll must not rewrite the file another process just wrote.
    assert store_path.read_bytes() == bytes_after_add
    assert clock.delays[-1] == CronService.RELOAD_CHECK_S

    # A later external job that is due sooner re-plans the next deadline.
    sooner = cli.add_job(
        name="sooner",
        schedule=CronSchedule(kind="at", at_ms=clock.now() + 1400),
        message="sooner",
    )
    clock.wake()
    await wait_for(lambda: len(clock.delays) == 3)
    # fake now advanced by 1s: the new job is 400ms away, below the poll cap.
    assert clock.delays[-1] == pytest.approx(0.4)

    clock.wake()
    await wait_for(lambda: len(clock.delays) == 4)
    assert dispatched == [sooner.id]

    # Further polls must not dispatch the same one-shot job again. The first
    # job is due later and runs exactly once as well.
    for _ in range(3):
        count = len(clock.delays)
        clock.wake()
        await wait_for(lambda: len(clock.delays) == count + 1)
    assert dispatched == [sooner.id, later.id]

    # A disabled job added externally is ignored.
    disabled = cli.add_job(
        name="disabled",
        schedule=CronSchedule(kind="at", at_ms=clock.now() + 500),
        message="disabled",
    )
    cli.enable_job(disabled.id, False)
    before = list(dispatched)
    count = len(clock.delays)
    clock.wake()
    await wait_for(lambda: len(clock.delays) > count)
    assert dispatched == before

    gateway.stop()
    assert gateway._timer_task is None
    count = len(clock.delays)
    clock.wake()
    await asyncio.sleep(0)
    assert len(clock.delays) == count
    assert dispatched == before


async def test_execution_merge_keeps_added_removed_and_disabled_jobs(
    tmp_path, monkeypatch
) -> None:
    """A long job must not clobber CLI changes made while it was running."""
    clock = _FakeCronClock()
    _install_cron_clock(monkeypatch, clock)
    store_path = tmp_path / "cron" / "jobs.json"

    calls: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def on_job(job):
        calls.append(job.id)
        if len(calls) == 1:
            entered.set()
            await release.wait()

    gateway = CronService(store_path)
    gateway.on_job = on_job
    await gateway.start()
    await wait_for(lambda: bool(clock.delays))

    own = gateway.add_job(
        name="own",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="own",
    )
    other = gateway.add_job(
        name="other",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="other",
    )
    executing = asyncio.create_task(
        gateway._execute_job(gateway._job_by_id(own.id))
    )
    await asyncio.wait_for(entered.wait(), 2)

    # A second process edits the same store while the job's model/tool call is
    # still running. A synchronous call in a worker thread proves the store
    # lock is not held across the await (otherwise this would time out).
    cli = CronService(store_path)
    added = await asyncio.wait_for(
        asyncio.to_thread(
            lambda: cli.add_job(
                name="added",
                schedule=CronSchedule(kind="every", every_ms=30_000),
                message="added",
            )
        ),
        2,
    )
    assert await asyncio.wait_for(
        asyncio.to_thread(lambda: cli.remove_job(other.id)), 2
    ) is True
    assert await asyncio.wait_for(
        asyncio.to_thread(lambda: cli.enable_job(own.id, False)), 2
    ) is not None

    release.set()
    await asyncio.wait_for(executing, 2)

    from_disk = {j.id: j for j in CronService(store_path).list_jobs(include_disabled=True)}
    assert added.id in from_disk  # new job kept
    assert other.id not in from_disk  # deleted job not revived
    assert from_disk[own.id].enabled is False  # external disable kept
    assert from_disk[own.id].state.next_run_at_ms is None
    assert {j.id for j in gateway.list_jobs(include_disabled=True)} == {
        own.id,
        added.id,
    }

    # The next poll must not dispatch the disabled job again.
    clock.wake()
    await wait_for(lambda: len(clock.delays) >= 2)
    assert calls == [own.id]

    gateway.stop()
    assert gateway._timer_task is None


async def test_adding_a_job_during_a_running_tick_does_not_abort_it(
    tmp_path, monkeypatch
) -> None:
    """Re-arming from another task must not cancel the job in flight."""
    clock = _FakeCronClock()
    _install_cron_clock(monkeypatch, clock)
    store_path = tmp_path / "cron" / "jobs.json"

    calls: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def on_job(job):
        calls.append(job.id)
        entered.set()
        await release.wait()

    service = CronService(store_path)
    service.on_job = on_job
    await service.start()
    await wait_for(lambda: bool(clock.delays))
    due = service.add_job(
        name="due",
        schedule=CronSchedule(kind="at", at_ms=clock.now() + 1000),
        message="due",
    )

    clock.wake()  # timer task is now inside _on_timer, awaiting the job
    await asyncio.wait_for(entered.wait(), 2)

    # A synchronous add from the same process while the tick runs: it must not
    # cancel the timer task or lose the new job.
    added = service.add_job(
        name="added",
        schedule=CronSchedule(kind="at", at_ms=clock.now() + 2000),
        message="added",
    )
    release.set()
    await wait_for(lambda: len(clock.delays) >= 2)

    assert calls == [due.id]
    from_disk = {j.id: j for j in CronService(store_path).list_jobs(include_disabled=True)}
    assert from_disk[due.id].state.last_status == "ok"  # the job really settled
    assert from_disk[due.id].enabled is False
    assert added.id in from_disk

    service.stop()
    assert service._timer_task is None


async def test_execution_merge_keeps_an_external_schedule_change(
    tmp_path, monkeypatch
) -> None:
    clock = _FakeCronClock()
    _install_cron_clock(monkeypatch, clock)
    store_path = tmp_path / "cron" / "jobs.json"

    calls: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def on_job(job):
        calls.append(job.id)
        entered.set()
        await release.wait()

    gateway = CronService(store_path)
    gateway.on_job = on_job
    await gateway.start()
    await wait_for(lambda: bool(clock.delays))
    own = gateway.add_job(
        name="own",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="own",
    )
    executing = asyncio.create_task(
        gateway._execute_job(gateway._job_by_id(own.id))
    )
    await asyncio.wait_for(entered.wait(), 2)

    cli = CronService(store_path)

    def rewrite_schedule() -> None:
        with cli._store_lock():
            cli._load_store()
            job = cli._job_by_id(own.id)
            job.schedule.every_ms = 3_600_000
            job.state.next_run_at_ms = clock.now() + 3_600_000
            cli._write_store()

    await asyncio.wait_for(asyncio.to_thread(rewrite_schedule), 2)

    release.set()
    await asyncio.wait_for(executing, 2)

    from_disk = {j.id: j for j in CronService(store_path).list_jobs()}[own.id]
    # The running engine computed a 60s next run; the external schedule wins.
    assert from_disk.schedule.every_ms == 3_600_000
    assert from_disk.state.next_run_at_ms == clock.now() + 3_600_000
    assert gateway._job_by_id(own.id).schedule.every_ms == 3_600_000

    clock.wake()
    await wait_for(lambda: len(clock.delays) >= 2)
    assert calls == [own.id]

    gateway.stop()
    assert gateway._timer_task is None


async def test_last_external_route_survives_a_restart(tmp_path, monkeypatch) -> None:
    """After a restart the heartbeat still targets the last external chat."""
    async def first_responder(index, messages, tools):
        return response("hello from the chat")

    first_provider = ScriptedProvider(first_responder)
    (
        config,
        bus,
        telegram,
        channels,
        cron,
        heartbeat,
        runtime_cm,
        _,
    ) = await _open_gateway(tmp_path, monkeypatch, first_provider)

    async with runtime_cm as runtime:
        gateway = _gateway(
            runtime, bus, channels, cron, heartbeat, workspace=config.workspace_path
        )
        serve = asyncio.create_task(gateway.serve())
        await wait_for(lambda: channels.started)
        await publish_inbound(
            bus,
            "telegram",
            "42",
            "hello",
            metadata={"message_thread_id": 7},
            session_key="telegram:42:topic:7",
        )
        await wait_for(lambda: telegram.sent)
        await gateway.aclose()
        serve.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve

    assert (config.workspace_path / ".gateway_route.json").is_file()

    # A new process: no channel input has happened yet.
    async def heartbeat_responder(index, messages, tools):
        if _is_heartbeat_decision(tools):
            return tool_call(
                "h1", "heartbeat", {"action": "run", "tasks": "check the plants"}
            )
        return response("plants look fine")

    second_provider = ScriptedProvider(heartbeat_responder)
    (
        config2,
        bus2,
        telegram2,
        channels2,
        cron2,
        heartbeat2,
        runtime_cm2,
        _,
    ) = await _open_gateway(
        tmp_path, monkeypatch, second_provider, heartbeat_provider=second_provider
    )
    (config2.workspace_path / "HEARTBEAT.md").write_text(
        "# Tasks\n- check the plants\n", encoding="utf-8"
    )

    async with runtime_cm2 as runtime:
        gateway = _gateway(
            runtime,
            bus2,
            channels2,
            cron2,
            heartbeat2,
            workspace=config2.workspace_path,
        )
        manager = asyncio.create_task(channels2.start_all())
        await heartbeat2._tick()
        await wait_for(lambda: telegram2.sent)
        await gateway.aclose()
        manager.cancel()
        with pytest.raises(asyncio.CancelledError):
            await manager

    assert [message.chat_id for message in telegram2.sent] == ["42"]
    assert telegram2.sent[0].content == "plants look fine"
    assert telegram2.sent[0].metadata["message_thread_id"] == 7


async def test_cron_job_runs_one_core_turn_and_delivers_once(
    tmp_path, monkeypatch
) -> None:
    async def responder(index, messages, tools):
        return response("drink water")

    provider = ScriptedProvider(responder)
    (
        config,
        bus,
        telegram,
        channels,
        cron,
        heartbeat,
        runtime_cm,
        _,
    ) = await _open_gateway(tmp_path, monkeypatch, provider)

    async with runtime_cm as runtime:
        gateway = _gateway(runtime, bus, channels, cron, heartbeat)
        manager = asyncio.create_task(channels.start_all())
        job = cron.add_job(
            name="hydrate",
            schedule=CronSchedule(kind="every", every_ms=3_600_000),
            message="drink water",
            deliver=True,
            channel="telegram",
            to="42",
        )
        assert await cron.run_job(job.id) is True
        await wait_for(lambda: telegram.sent)
        await asyncio.sleep(0.05)

        assert len(provider.calls) == 1
        assert "[Scheduled Task] Timer finished." in json.dumps(
            provider.calls[0]["messages"], ensure_ascii=False
        )
        assert [message.content for message in telegram.sent] == ["drink water"]
        assert await _catalog_session_ids(runtime) == [f"cron:{job.id}"]

        await gateway.aclose()
        manager.cancel()
        with pytest.raises(asyncio.CancelledError):
            await manager


async def test_cron_tool_and_scheduler_share_one_service_instance(
    tmp_path, monkeypatch
) -> None:
    """A job created by the tool is the job the gateway scheduler runs."""
    async def responder(index, messages, tools):
        if index == 1:
            return tool_call(
                "c1",
                "cron",
                {"action": "add", "message": "stretch", "every_seconds": 3600},
            )
        return response(f"reply-{index}")

    provider = ScriptedProvider(responder)
    (
        config,
        bus,
        telegram,
        channels,
        cron,
        heartbeat,
        runtime_cm,
        _,
    ) = await _open_gateway(tmp_path, monkeypatch, provider)

    async with runtime_cm as runtime:
        gateway = _gateway(runtime, bus, channels, cron, heartbeat)
        serve = asyncio.create_task(gateway.serve())
        await wait_for(lambda: channels.started)
        await publish_inbound(bus, "telegram", "42", "remind me hourly")
        await wait_for(lambda: telegram.sent)
        jobs = cron.list_jobs()
        assert len(jobs) == 1

        # The same instance reports and runs the job the tool created.
        assert await cron.run_job(jobs[0].id) is True
        await wait_for(lambda: len(telegram.sent) == 2)
        assert "stretch" in str(provider.calls[-1]["messages"])

        await gateway.aclose()
        serve.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve


async def test_restart_does_not_replay_a_past_due_cron_job(
    tmp_path, monkeypatch
) -> None:
    store = tmp_path / "cron" / "jobs.json"
    store.parent.mkdir(parents=True)
    store.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "old",
                        "name": "old action",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 3_600_000},
                        "payload": {
                            "kind": "agent_turn",
                            "message": "do the old thing",
                            "deliver": True,
                            "channel": "telegram",
                            "to": "42",
                        },
                        "state": {
                            "nextRunAtMs": 1,
                            "lastRunAtMs": None,
                            "lastStatus": None,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    async def responder(index, messages, tools):
        return response("should not run")

    provider = ScriptedProvider(responder)
    (
        config,
        bus,
        telegram,
        channels,
        cron,
        heartbeat,
        runtime_cm,
        _,
    ) = await _open_gateway(tmp_path, monkeypatch, provider)

    async with runtime_cm as runtime:
        gateway = _gateway(runtime, bus, channels, cron, heartbeat)
        manager = asyncio.create_task(channels.start_all())
        await gateway.cron.start()
        await asyncio.sleep(0.1)
        assert provider.calls == []
        assert telegram.sent == []
        await gateway.aclose()
        manager.cancel()
        with pytest.raises(asyncio.CancelledError):
            await manager


async def test_close_stops_intake_scheduling_and_settles_turns(
    tmp_path, monkeypatch
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def responder(index, messages, tools):
        entered.set()
        await release.wait()
        return response("late")

    provider = ScriptedProvider(responder)
    (
        config,
        bus,
        telegram,
        channels,
        cron,
        heartbeat,
        runtime_cm,
        _,
    ) = await _open_gateway(tmp_path, monkeypatch, provider)

    async with runtime_cm as runtime:
        gateway = _gateway(runtime, bus, channels, cron, heartbeat)
        serve = asyncio.create_task(gateway.serve())
        await wait_for(lambda: channels.started)
        await publish_inbound(bus, "telegram", "42", "blocked")
        await asyncio.wait_for(entered.wait(), 2)

        await asyncio.wait_for(gateway.aclose(), 3)
        serve.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve

        assert runtime.closed is True
        assert runtime.unsettled == 0
        assert channels.stopped is True
        assert cron.status()["enabled"] is False
        assert telegram.sent == []  # interrupted, not reported as an answer
        release.set()


async def test_run_core_gateway_wires_the_production_entry(
    tmp_path, monkeypatch
) -> None:
    """The real entry builds one shared service set and serves channel input."""
    from io import StringIO

    from rich.console import Console

    config = make_config(tmp_path)
    coordinator = FakeCoordinator()
    monkeypatch.setattr(
        agent_commands, "build_forge_components", fake_forge_builder(coordinator)
    )

    async def responder(index, messages, tools):
        return response(f"answer-{index}")

    provider = ScriptedProvider(responder)
    bus = MessageBus()
    telegram = FakeChannel("telegram")
    channels = FakeChannelManager({"telegram": telegram}, bus)
    cron = CronService(tmp_path / "cron" / "jobs.json")

    def heartbeat_factory(*, config, provider, model):
        return HeartbeatService(
            workspace=config.workspace_path,
            provider=provider,
            model=model,
            interval_s=3600,
            enabled=False,
        )

    gateway = asyncio.create_task(
        run_core_gateway(
            config=config,
            port=1,
            bus=bus,
            channel_manager=channels,
            heartbeat_factory=heartbeat_factory,
            cron=cron,
            service=ProviderService(config),
            selected=make_selection(provider),
            console_out=Console(file=StringIO()),
        )
    )
    await wait_for(lambda: channels.started)
    await publish_inbound(bus, "telegram", "42", "hello")
    await wait_for(lambda: telegram.sent)
    assert [message.content for message in telegram.sent] == ["answer-1"]

    gateway.cancel()
    with pytest.raises(asyncio.CancelledError):
        await gateway
    assert channels.stopped is True
    assert cron.status()["enabled"] is False


async def test_core_gateway_does_not_import_the_legacy_package() -> None:
    code = """
import sys
BLOCKED = ("PhyAgentOS.agent_legacy", "PhyAgentOS.cli.legacy")
class Block:
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == p or fullname.startswith(p + ".") for p in BLOCKED):
            raise ImportError("blocked legacy import: " + fullname)
        return None
sys.meta_path.insert(0, Block())
from typer.testing import CliRunner
from PhyAgentOS.cli.commands import app
result = CliRunner().invoke(app, ["gateway", "--help"])
assert result.exit_code == 0, result.output
assert "core (default)" in result.output, result.output
loaded = [m for m in sys.modules if any(m == p or m.startswith(p + ".") for p in BLOCKED)]
assert loaded == [], loaded
print("ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "ok" in completed.stdout


async def test_legacy_gateway_runs_only_when_selected(monkeypatch) -> None:
    from typer.testing import CliRunner

    import PhyAgentOS.cli.commands as commands
    import PhyAgentOS.cli.legacy.legacy_gateway as legacy_gateway

    calls: list[dict] = []

    def fake_run_gateway(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(legacy_gateway, "run_gateway", fake_run_gateway)
    result = CliRunner().invoke(commands.app, ["gateway", "--runtime", "legacy"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1


async def test_unknown_runtime_is_refused() -> None:
    from typer.testing import CliRunner

    import PhyAgentOS.cli.commands as commands

    result = CliRunner().invoke(commands.app, ["gateway", "--runtime", "loop"])
    assert result.exit_code != 0
    assert "choose core or legacy" in result.output


async def test_fleet_mode_is_explicitly_unsupported(tmp_path) -> None:
    config = Config()
    config.embodiments.mode = "fleet"
    config.embodiments.shared_workspace = str(tmp_path / "shared")
    with pytest.raises(ValueError, match="fleet"):
        await run_core_gateway(config=config, port=1)
