"""Run Planner/Actor sessions through the existing runtime and target contracts."""

from __future__ import annotations

import json
import math
import os
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

import typer
import yaml

from PhyAgentOS.game_agents.stardew import register_general_game
from PhyAgentOS.providers.custom_provider import CustomProvider
from PhyAgentOS.runtime.preflight.runtime_compatibility_preflight import (
    RuntimeCompatibilityPreflight,
)
from PhyAgentOS.runtime.schemas import SessionSpec, SkillRuntimeSpec, TargetSpec
from PhyAgentOS.runtime.sessions.session_runner import SessionRunner
from PhyAgentOS.runtime.watchdog.runtime_registry import SkillRuntimeRegistry, TargetRuntimeRegistry
from PhyAgentOS.runtime.watchdog.scheduler import ScheduledSession


def success_check(expected):
    if not isinstance(expected, dict) or not expected:
        raise ValueError("success checks must be a nonempty {observation.path: value} object")
    for path, value in expected.items():
        if not isinstance(path, str) or not all(path.split(".")):
            raise ValueError("success checks require nonempty observation paths")
        if isinstance(value, dict) and "$lt" in value:
            limit = value["$lt"]
            if set(value) != {"$lt"} or type(limit) not in (int, float) or not math.isfinite(limit):
                raise ValueError("$lt requires a single finite numeric upper bound")

    def verify(observation, feedback):
        for path, expected_value in expected.items():
            value = observation
            for key in path.split("."):
                if not isinstance(value, dict) or key not in value:
                    return False
                value = value[key]
            if isinstance(expected_value, dict) and "$lt" in expected_value:
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or not value < expected_value["$lt"]
                ):
                    return False
            elif value != expected_value:
                return False
        return True

    return verify


def run_general_game(
    workspace: Path = typer.Option(..., help="Session workspace directory"),
    target: Path = typer.Option(..., help="TargetSpec YAML"),
    session: Path = typer.Option(..., help="SessionSpec YAML"),
    actions: Path = typer.Option(..., help="Action catalog JSON"),
    model: str = typer.Option(..., help="Model name"),
    api_base: str = typer.Option("http://localhost:8000/v1", help="OpenAI-compatible API base"),
    evolve: bool = typer.Option(False, help="Record unverified memory candidates after execution"),
    success_checks: Path | None = typer.Option(None, help="Observed completion conditions JSON"),
) -> None:
    """Execute a configured game task with bounded Planner/Actor rounds."""
    # Core resolves relative contract paths through this directory, even on first run.
    workspace.mkdir(parents=True, exist_ok=True)
    target = TargetSpec.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))
    session = SessionSpec.model_validate(yaml.safe_load(session.read_text(encoding="utf-8")))
    skill = SkillRuntimeSpec.model_validate(
        yaml.safe_load(
            files("PhyAgentOS")
            .joinpath("templates/configs/skillruntimes/general_game.yaml")
            .read_text(encoding="utf-8"),
        )
    )
    if session.skillruntime_ref.removeprefix("skillruntime://") != skill.id:
        raise typer.BadParameter("session.skillruntime_ref must reference general_game")
    if session.target_ref.removeprefix("target://") != target.id:
        raise typer.BadParameter("session.target_ref must match the supplied target")
    verify = (
        success_check(json.loads(success_checks.read_text(encoding="utf-8")))
        if success_checks
        else None
    )
    if (
        target.runtime.target_runtime
        in {
            "StardewValleyTargetRuntime",
            "MinecraftTargetRuntime",
        }
        and verify is None
    ):
        raise typer.BadParameter(
            "native game targets require --success-checks to verify task completion"
        )

    @asynccontextmanager
    async def provider_factory():
        provider = CustomProvider(
            api_key=os.environ.get("GAME_AGENT_API_KEY", "no-key"),
            api_base=api_base,
            default_model=model,
        )
        # CustomProvider owns this client; Core has no provider-wide close API.
        async with provider._client:
            yield provider

    register_general_game(
        provider_factory,
        model=model,
        action_catalog=json.loads(actions.read_text(encoding="utf-8")),
        memory_workspace=workspace,
        evolve=evolve,
        verify=verify,
    )
    scheduled = ScheduledSession(session, target, skill, target.id, skill.id)
    preflight = RuntimeCompatibilityPreflight(workspace).check(scheduled)
    if preflight.verdict != "accepted":
        raise typer.BadParameter(preflight.model_dump_json(indent=2))
    runner = SessionRunner(
        session=session,
        target_spec=target,
        skillruntime_spec=skill,
        adapter_plan=preflight.adapter_plan,
        target=TargetRuntimeRegistry().build(
            target,
            target_endpoint=session.routing.target_endpoint or target.runtime.target_endpoint,
        ),
        skill_runtime=SkillRuntimeRegistry().build(skill.runtime),
        policy_client=None,
        perception_runtime=None,
        perception_plan=None,
    )
    try:
        result = runner.start()
        print(result.model_dump_json(indent=2))
        if not result.success:
            raise typer.Exit(1)
    finally:
        runner.close()
