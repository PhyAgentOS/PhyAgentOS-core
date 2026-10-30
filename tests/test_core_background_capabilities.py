"""Native cron/message capabilities over the shared Core runtime.

Every test here opens the real Core session through the CLI's
``open_core_session`` (with the gateway's extra capabilities) and asserts what
the model and the tools actually did. Only the provider and the Forge
coordinator are fakes.
"""

from __future__ import annotations

import pytest
from core_background_support import (
    FakeCoordinator,
    ScriptedProvider,
    fake_forge_builder,
    make_config,
    make_selection,
    response,
    tool_call,
)

from PhyAgentOS.agent.capabilities.cron_capability import REENTRANCY_ERROR, CronCapability
from PhyAgentOS.agent.capabilities.message_capability import MessageCapability
from PhyAgentOS.agent.core import TurnRequest
from PhyAgentOS.agent.services.background_runtime import (
    TurnBinding,
    TurnRoutes,
    open_background_core,
)
from PhyAgentOS.bus.queue import MessageBus
from PhyAgentOS.cli.command_groups import agent_commands
from PhyAgentOS.cron.service import CronService
from PhyAgentOS.providers.service import ProviderService

pytestmark = pytest.mark.asyncio


def _script(*responses):
    async def responder(index, messages, tools):
        return responses[min(index - 1, len(responses) - 1)]

    return ScriptedProvider(responder)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    coordinator = FakeCoordinator()
    monkeypatch.setattr(
        agent_commands, "build_forge_components", fake_forge_builder(coordinator)
    )
    return config, coordinator


def _open(config, provider, *, cron, bus):
    routes = TurnRoutes()

    async def send(message):
        await bus.publish_outbound(message)

    capabilities = (CronCapability(cron, routes), MessageCapability(routes, send))
    return open_background_core(
        config,
        service=ProviderService(config),
        selected=make_selection(provider),
        capabilities=capabilities,
        routes=routes,
    )


def _binding(**metadata) -> TurnBinding:
    return TurnBinding(
        channel="telegram", chat_id="42", reply_to="99", metadata=metadata
    )


async def test_cron_tool_add_uses_the_turn_target_and_shared_store(
    tmp_path, workspace
) -> None:
    config, _ = workspace
    provider = _script(
        tool_call("c1", "cron", {"action": "add", "message": "water the plants", "every_seconds": 60}),
        response("scheduled"),
    )
    cron = CronService(tmp_path / "cron" / "jobs.json")
    bus = MessageBus()

    async with _open(config, provider, cron=cron, bus=bus) as runtime:
        outcome = await runtime.run_turn(
            session_id="telegram:42",
            text="remind me",
            binding=_binding(),
            source="test",
        )

    assert outcome.result.status.value == "completed"
    jobs = cron.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].payload.message == "water the plants"
    assert jobs[0].payload.deliver is True
    assert (jobs[0].payload.channel, jobs[0].payload.to) == ("telegram", "42")
    # The tool really was offered to the model in the Core request.
    tool_names = {tool["function"]["name"] for tool in provider.calls[0]["tools"]}
    assert {"cron", "message", "forge_task_create", "activate_skill"} <= tool_names


async def test_cron_tool_refuses_to_schedule_inside_a_cron_turn(
    tmp_path, workspace
) -> None:
    config, _ = workspace
    provider = _script(
        tool_call("c1", "cron", {"action": "add", "message": "nested", "every_seconds": 5}),
        response("refused"),
    )
    cron = CronService(tmp_path / "cron" / "jobs.json")
    bus = MessageBus()

    async with _open(config, provider, cron=cron, bus=bus) as runtime:
        outcome = await runtime.run_turn(
            session_id="cron:job-1",
            text="[Scheduled Task] run",
            binding=TurnBinding(
                channel="telegram", chat_id="42", scheduled=True
            ),
            source="cron:job-1",
        )

    assert outcome.result.status.value == "completed"
    assert cron.list_jobs() == []
    # The model saw the guard's reason, not a generic failure.
    tool_messages = [
        message
        for message in provider.calls[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert tool_messages
    assert REENTRANCY_ERROR in tool_messages[-1]["content"]


async def test_message_tool_sends_once_and_marks_the_turn_answered(
    tmp_path, workspace
) -> None:
    config, _ = workspace
    provider = _script(
        tool_call("m1", "message", {"content": "only once"}),
        response("this must be suppressed"),
    )
    cron = CronService(tmp_path / "cron" / "jobs.json")
    bus = MessageBus()

    async with _open(config, provider, cron=cron, bus=bus) as runtime:
        outcome = await runtime.run_turn(
            session_id="telegram:42",
            text="say it",
            binding=_binding(message_thread_id=7),
            source="test",
        )
        # The entry point reads this flag instead of delivering the final text.
        assert outcome.reply_sent is True

    outbound = []
    while not bus.outbound.empty():
        outbound.append(bus.outbound.get_nowait())
    assert [message.content for message in outbound] == ["only once"]
    assert outbound[0].metadata["message_thread_id"] == 7
    assert outbound[0].reply_to == "99"


async def test_message_tool_keeps_media_paths(tmp_path, workspace) -> None:
    config, _ = workspace
    provider = _script(
        tool_call(
            "m1",
            "message",
            {"content": "photo", "media": ["/tmp/photo.png", "voice.ogg"]},
        ),
        response("done"),
    )
    cron = CronService(tmp_path / "cron" / "jobs.json")
    bus = MessageBus()

    async with _open(config, provider, cron=cron, bus=bus) as runtime:
        await runtime.run_turn(
            session_id="telegram:42",
            text="send the picture",
            binding=_binding(),
            source="test",
        )

    outbound = []
    while not bus.outbound.empty():
        outbound.append(bus.outbound.get_nowait())
    assert len(outbound) == 1
    assert outbound[0].media == ["/tmp/photo.png", "voice.ogg"]


async def test_message_tool_reports_an_unknown_target_to_the_model(
    tmp_path, monkeypatch
) -> None:
    config = make_config(tmp_path)
    coordinator = FakeCoordinator()
    monkeypatch.setattr(
        agent_commands, "build_forge_components", fake_forge_builder(coordinator)
    )
    provider = _script(
        tool_call("m1", "message", {"content": "hello", "channel": "slack"}),
        response("could not send"),
    )
    cron = CronService(tmp_path / "cron" / "jobs.json")
    bus = MessageBus()
    routes = TurnRoutes()

    async def send(message):
        raise ValueError(f"unknown channel '{message.channel}'; it is not enabled")

    capabilities = (CronCapability(cron, routes), MessageCapability(routes, send))
    async with open_background_core(
        config,
        service=ProviderService(config),
        selected=make_selection(provider),
        capabilities=capabilities,
        routes=routes,
    ) as runtime:
        outcome = await runtime.run_turn(
            session_id="telegram:42",
            text="send it",
            binding=_binding(),
            source="test",
        )

    assert outcome.result.status.value == "completed"
    assert outcome.reply_sent is False
    tool_messages = [
        message
        for message in provider.calls[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert "unknown channel 'slack'" in tool_messages[-1]["content"]
    assert bus.outbound.empty()


class _NoopOutput:
    """The CLI output surface Core requires, without a terminal."""

    def __init__(self) -> None:
        self.events = []
        self.errors = []

    def event(self, event) -> None:
        self.events.append(event)

    def error(self, text: str) -> None:
        self.errors.append(text)


async def test_cli_cron_capability_adds_jobs_without_starting_the_scheduler(
    tmp_path, workspace
) -> None:
    """A plain CLI Core turn may create a job; no scheduler runs in this process."""
    config, _ = workspace
    provider = _script(
        tool_call(
            "c1",
            "cron",
            {"action": "add", "message": "tomorrow reminder", "every_seconds": 3600},
        ),
        response("created"),
    )
    store_path = tmp_path / "cron" / "jobs.json"
    cron = CronService(store_path)
    output = _NoopOutput()

    async with agent_commands.open_core_session(
        config,
        output=output,
        additional_capabilities=(CronCapability(cron),),
        service=ProviderService(config),
        selected=make_selection(provider),
    ) as session:
        result = await session.submit(
            TurnRequest("cli:direct", "remind me tomorrow")
        )

    assert result.status.value == "completed"
    jobs = cron.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].payload.message == "tomorrow reminder"
    # The CLI-only capability records its explicit target and does not start
    # any timer in this process.
    assert (jobs[0].payload.channel, jobs[0].payload.to) == ("cli", "direct")
    assert cron.status()["enabled"] is False
    # A gateway process reading the same store sees the job the CLI created.
    assert [job.name for job in CronService(store_path).list_jobs()] == [
        "tomorrow reminder"
    ]


async def test_forge_blocker_reaches_the_background_prompt(tmp_path) -> None:
    """The migrated entry keeps the Core/Forge unresolved-unknown guard."""
    config = make_config(tmp_path)
    blocker = {
        "task_id": "task-1",
        "invocation_id": None,
        "status": "unknown",
        "semantics": "action",
    }
    coordinator = FakeCoordinator(blockers=[blocker])
    from PhyAgentOS.cli.command_groups import agent_commands

    original = agent_commands.build_forge_components
    agent_commands.build_forge_components = fake_forge_builder(coordinator)
    try:
        provider = _script(response("no action"))
        cron = CronService(tmp_path / "cron" / "jobs.json")
        bus = MessageBus()
        async with _open(config, provider, cron=cron, bus=bus) as runtime:
            await runtime.run_turn(
                session_id="heartbeat",
                text="check tasks",
                binding=TurnBinding(channel="cli", chat_id="direct"),
                source="heartbeat",
            )
    finally:
        agent_commands.build_forge_components = original

    system_text = "\n".join(
        message["content"] or ""
        for message in provider.calls[0]["messages"]
        if message.get("role") == "system"
    )
    assert 'invocation_id=None' in system_text
    assert "blocks creating a new AgentTask" in system_text
    assert coordinator.reconcile_calls == 1
