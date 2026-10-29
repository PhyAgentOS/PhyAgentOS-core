"""Composition root for a Forge-enabled agent host.

This is the one production-side place that builds the standard core objects
for an entry point:

    AgentHostImpl -> AgentRuntimeImpl -> LoopDriverImpl -> ToolExecutorImpl
                  -> ForgeCapability -> legacy Forge task/Tool API wrappers
                  -> AgentTaskCoordinator / ForgeToolClient

Every collaborator is injected: this module creates no provider, session
store, Forge client, or background task, and reads no global config. The
returned :class:`AgentAssembly` owns shutdown through ``host.aclose()``, which
closes the runtime and then the injected store in the contract order.

The Forge capability deliberately keeps using the existing wrappers in
:mod:`PhyAgentOS.agent_legacy.tools`. Those wrappers hold the accepted Gateway
call shapes and the only physical execution path
(``ForgeToolClient -> Gateway Tool API -> ToolEndpoint``); duplicating them
here would create a second implementation. The dependency is explicit in
:mod:`PhyAgentOS.agent.capabilities.forge` and covered by the capability
integration tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from PhyAgentOS.forge.task import AgentTaskCoordinator
from PhyAgentOS.forge.tool_client import ForgeToolClient

from .capabilities.forge import ForgeCapability
from .core import (
    AgentHostImpl,
    AgentRuntimeImpl,
    Capability,
    LoopDriverImpl,
    ModelAdapterImpl,
    PromptAssemblerImpl,
    SessionStore,
    ToolExecutorImpl,
)
from .core.model import ChatProvider

__all__ = ["AgentAssembly", "compose_agent"]


@dataclass(frozen=True, slots=True)
class AgentAssembly:
    """The built core objects and the host that owns their shutdown."""

    host: AgentHostImpl
    runtime: AgentRuntimeImpl
    store: SessionStore
    executor: ToolExecutorImpl
    capabilities: tuple[Capability, ...]


def compose_agent(
    *,
    provider: ChatProvider,
    store: SessionStore,
    coordinator: AgentTaskCoordinator,
    client: ForgeToolClient | None = None,
    system_instructions: str,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    tool_choice: str | None = None,
    max_steps: int = 16,
    deadline_seconds: float | None = None,
) -> AgentAssembly:
    """Build the Forge-enabled host from already-built collaborators.

    ``provider`` is anything exposing the provider's
    ``chat_with_retry`` coroutine; it is wrapped by :class:`ModelAdapterImpl`.
    ``store``, ``coordinator``, and ``client`` are injected and remain owned by
    the caller's process, except that ``host.aclose()`` closes the store
    through the runtime as the core contract requires. ``system_instructions``
    and the generation parameters are the core's own configuration; none of
    them is read from a file or environment here.
    """
    capability = ForgeCapability(coordinator, client=client)
    capabilities: tuple[Capability, ...] = (capability,)

    registrations = tuple(
        registration
        for item in capabilities
        for registration in item.tool_registrations()
    )
    definitions = tuple(
        registration.definition for registration in registrations
    )

    assembler = PromptAssemblerImpl(
        system_instructions,
        fragments=tuple(item.prompt_context for item in capabilities),
    )
    adapter = ModelAdapterImpl(provider)
    executor = ToolExecutorImpl(registrations, store)
    driver = LoopDriverImpl(
        assembler=assembler,
        adapter=adapter,
        executor=executor,
        store=store,
        tools=definitions,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        tool_choice=tool_choice,
    )
    runtime = AgentRuntimeImpl(
        driver=driver,
        store=store,
        max_steps=max_steps,
        deadline_seconds=deadline_seconds,
    )
    host = AgentHostImpl(runtime=runtime, capabilities=capabilities)
    return AgentAssembly(
        host=host,
        runtime=runtime,
        store=store,
        executor=executor,
        capabilities=capabilities,
    )
