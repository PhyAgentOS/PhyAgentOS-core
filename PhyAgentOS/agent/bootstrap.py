"""Build Forge dependencies for entry points without constructing a loop.

Both transitional and Core entry points use these factories. Configuration
is resolved here, never inside Core or the Forge tool adapters.
"""

from __future__ import annotations

import asyncio

from PhyAgentOS.config.schema import Config
from PhyAgentOS.forge.binding import ForgeSkillBindingResolver
from PhyAgentOS.forge.task import AgentTaskCoordinator
from PhyAgentOS.providers.service import ProviderService
from PhyAgentOS.skill_runtime.catalog import SkillCatalog
from PhyAgentOS.skill_runtime.integration import (
    ActiveRuntimeRegistry,
    DynamicForgeToolClient,
    DynamicRuntimeSet,
    discover_active_runtime,
)
from PhyAgentOS.skill_runtime.manager import RuntimeManager
from PhyAgentOS.skill_runtime.state import RuntimeStateStore

from .services.forge_verifier import ForgeTaskVerifier
from .services.skill_documents import SkillDocumentLoader


def build_forge_verifier(config: Config, provider, *, write_legacy_lessons=None):
    if not config.agents.verification.service_enabled:
        return None
    settings = config.agents.verification
    model = settings.model or config.agents.defaults.model
    service = ProviderService(config)
    provider_name = service.background_provider_name(settings.provider, settings.model)
    if not provider_name:
        raise RuntimeError(f"cannot resolve verification provider for model {model!r}")
    selection = service.selection(
        provider_name, model, service.background_effort(provider_name, model) or "none"
    )
    provider_config = getattr(service.config.providers, selection.name)
    child_spec = {
        "provider_name": selection.name,
        "model": selection.model,
        "api_key": provider_config.api_key,
        "api_base": selection.endpoint,
        "extra_headers": provider_config.extra_headers,
        "max_retries": provider_config.max_retries,
        "temperature": 0.0,
        "max_tokens": min(4096, config.agents.defaults.max_tokens),
        "reasoning_effort": selection.effort,
    }
    return ForgeTaskVerifier(
        workspace=config.workspace_path,
        provider=provider,
        model=model,
        evidence_retention=settings.evidence_retention,
        timeout_s=settings.timeout_s,
        service_host=settings.service_host,
        service_port=settings.service_port,
        service_provider_spec=child_spec,
        max_calls=settings.max_verifier_calls_per_run,
        write_legacy_lessons=(
            not config.agents.evolution.enabled
            if write_legacy_lessons is None else write_legacy_lessons
        ),
    )


def build_forge_components(
    config: Config, provider, *, write_legacy_lessons=None, resources=None
):
    catalog = SkillCatalog()
    state_store = RuntimeStateStore()
    manager = RuntimeManager(catalog=catalog, state_store=state_store)
    active = discover_active_runtime(catalog=catalog, state_store=state_store, manager=manager)
    registry = ActiveRuntimeRegistry(
        active, catalog=catalog, state_store=state_store, auto_refresh=True
    )
    client = DynamicForgeToolClient(registry)
    if resources is not None:
        resources.push_async_callback(client.close)
    verifier = build_forge_verifier(
        config, provider, write_legacy_lessons=write_legacy_lessons
    )
    if resources is not None and verifier is not None:
        resources.push_async_callback(asyncio.to_thread, verifier.stop)
    invocation_ids = DynamicRuntimeSet(registry, "invocation_ids")
    resolver = ForgeSkillBindingResolver(
        registry,
        catalog=catalog,
        skills_loader=SkillDocumentLoader(
            config.workspace_path, runtime_availability_provider=registry.is_available
        ),
    )
    coordinator = AgentTaskCoordinator(
        workspace=config.workspace_path,
        config=config.forge,
        client=client,
        binding_resolver=resolver,
        runtime_invocation_ids=invocation_ids,
        runtime_session_ids=DynamicRuntimeSet(registry, "session_ids"),
        runtime_task_binding_ids=DynamicRuntimeSet(registry, "task_binding_ids"),
        verifier=verifier,
        max_replans=config.agents.verification.max_replans_per_episode,
        replan_timeout_s=config.agents.verification.replan_timeout_s,
    )
    return client, invocation_ids, coordinator, registry.is_available
