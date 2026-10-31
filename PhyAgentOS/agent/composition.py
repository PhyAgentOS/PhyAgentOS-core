"""Composition root for a Forge-enabled agent host.

This is the one production-side place that builds the standard core objects
for an entry point:

    AgentHostImpl -> AgentRuntimeImpl -> LoopDriverImpl -> ToolExecutorImpl
                  -> ForgeCapability -> native Forge task/Tool API adapters
                  -> AgentTaskCoordinator / ForgeToolClient

Every collaborator is injected: this module creates no provider, session
store, Forge client, or background task, and reads no global config. The
returned :class:`AgentAssembly` owns shutdown through ``host.aclose()``, which
closes the runtime and then the injected store in the contract order.

The Forge capability uses the native adapter modules in
:mod:`PhyAgentOS.agent.capabilities`; it does not import
:mod:`PhyAgentOS.agent_legacy`. Those adapters hold the accepted Gateway call
shapes and the only physical execution path
(``ForgeToolClient -> Gateway Tool API -> ToolEndpoint``). The legacy tool
classes delegate to the same definitions and operations, so the two paths
cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from PhyAgentOS.forge.task import AgentTaskCoordinator
from PhyAgentOS.forge.tool_client import ForgeToolClient

from .capabilities.forge_capability import ForgeCapability
from .capabilities.skill_access_capability import SkillAccessCapability
from .core import (
    AgentHostImpl,
    AgentRuntimeImpl,
    Capability,
    ExecutionContext,
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
    additional_capabilities: Sequence[Capability] = (),
    forge_capability: Capability | None = None,
    stream: bool = True,
    max_parallel_tools: int = 4,
    cache_stable_prompts: bool = False,
    context_policy: Any = None,
    turn_budget: Any = None,
    steering: Any = None,
) -> AgentAssembly:
    """Build the Forge-enabled host from already-built collaborators.

    ``provider`` is anything exposing the provider's
    ``chat_with_retry`` coroutine; it is wrapped by :class:`ModelAdapterImpl`.
    When it also exposes ``chat_stream``, ``stream=True`` (the default) lets
    the core display body text before the model finishes; providers without it
    keep the non-streaming path unchanged. ``stream=False`` disables native
    streaming explicitly.
    ``store``, ``coordinator``, and ``client`` are injected and remain owned by
    the caller's process, except that ``host.aclose()`` closes the store
    through the runtime as the core contract requires. ``system_instructions``
    and the generation parameters are the core's own configuration; none of
    them is read from a file or environment here.

    ``additional_capabilities`` are registered in the given order and receive
    the prompt fragment hook before Forge. ``forge_capability`` may be an
    already-built Forge capability so several assemblies can share one
    coordinator/client pair; when omitted this function builds the standard
    one. Forge always stays last, so capability order is stable for both
    callers.
    """
    capability = forge_capability or ForgeCapability(coordinator, client=client)
    # Executor-bound capabilities must not share a mutable executor reference
    # across the entry's concurrent per-turn assemblies.
    additional_capabilities = tuple(
        item.for_turn() if callable(getattr(item, "for_turn", None)) else item
        for item in additional_capabilities
    )
    capabilities: tuple[Capability, ...] = (*additional_capabilities, capability)

    skill_capability = next(
        (item for item in additional_capabilities if isinstance(item, SkillAccessCapability)),
        None,
    )
    if skill_capability is not None and isinstance(capability, ForgeCapability):
        def _stage(context: ExecutionContext) -> str:
            if not skill_capability.has_primary_activation(context.session_id, context.turn_id):
                return "none"
            return "task" if capability.has_active_task(context.session_id) else "activated"

        capability.set_stage_resolver(_stage)

    visibility = tuple(
        item.hidden_tools
        for item in capabilities
        if callable(getattr(item, "hidden_tools", None))
    )

    def hidden_tools(context: ExecutionContext) -> frozenset[str]:
        return frozenset(name for hide in visibility for name in hide(context))

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
        hidden_tools=hidden_tools if visibility else None,
        cache_stable=cache_stable_prompts,
    )
    adapter = ModelAdapterImpl(provider, stream=stream)
    executor = ToolExecutorImpl(registrations, store)
    for item in capabilities:
        binder = getattr(item, "bind_executor", None)
        if callable(binder):
            binder(executor)
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
        max_parallel_tools=max_parallel_tools,
        context_policy=context_policy,
        turn_budget=turn_budget,
        steering=steering,
    )
    runtime = AgentRuntimeImpl(
        driver=driver,
        store=store,
        max_steps=max_steps,
        deadline_seconds=deadline_seconds,
        steering=steering,
    )
    host = AgentHostImpl(runtime=runtime, capabilities=capabilities)
    return AgentAssembly(
        host=host,
        runtime=runtime,
        store=store,
        executor=executor,
        capabilities=capabilities,
    )
