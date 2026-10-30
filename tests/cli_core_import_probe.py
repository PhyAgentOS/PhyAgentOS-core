"""Subprocess probe: the real ``paos agent`` entry must run Core without legacy.

Run as ``python tests/cli_core_import_probe.py <temp-dir>``. It installs an
import hook that fails on any ``agent_legacy`` or ``cli.legacy`` import, builds
the real Typer app from ``PhyAgentOS.cli.commands``, and invokes the default and
explicit-Core selections through ``CliRunner``. Only the external model provider
and the Forge/Gateway construction are replaced; the command dispatch, config
loading, Core composition, turn execution and session storage stay real.

This file is a test helper, not a collected test module.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_BLOCKED_PREFIXES = (
    "PhyAgentOS.agent_legacy",
    "PhyAgentOS.cli.legacy",
)


class _BlockLegacy:
    def find_spec(self, fullname, path=None, target=None):
        if any(
            fullname == prefix or fullname.startswith(f"{prefix}.")
            for prefix in _BLOCKED_PREFIXES
        ):
            raise ImportError(f"blocked legacy import: {fullname}")
        return None


PROVIDER_CALLS: list[list[dict]] = []


class _FakeProvider:
    async def chat_with_retry(self, *, messages, tools=None, **kwargs):
        from PhyAgentOS.providers.base import LLMResponse, ToolCallRequest

        PROVIDER_CALLS.append(list(messages))
        assert "read_file" in {item["function"]["name"] for item in tools}
        if messages[-1]["role"] == "user" and messages[-1]["content"] == "migration-read":
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="read-probe", name="read_file", arguments={"path": "probe.txt"},
                )],
                finish_reason="tool_calls",
            )
        if messages[-1]["role"] == "tool":
            assert "migration-content" in messages[-1]["content"]
            return LLMResponse(content="tool-read-confirmed", finish_reason="stop")
        return LLMResponse(content="done", finish_reason="stop")


class _FakeService:
    def __init__(self, config) -> None:
        del config

    def resolve(self, name=None, model=None, effort=None):
        from PhyAgentOS.providers.service import RuntimeSelection

        return RuntimeSelection(
            name or "fake", model or "fake-model", effort, None, _FakeProvider(), 100000
        )


class _FakeCoordinator:
    def __init__(self) -> None:
        self.binding_resolver = None
        self.store = SimpleNamespace(latest_for_session=lambda session: None)

    async def reconcile_nonterminal(self) -> None:
        return None

    def set_activation_manager(self, manager) -> None:
        del manager

    def unknown_remote_blockers(self) -> list[dict]:
        return []


def _fake_build_forge_components(
    config, provider, *, write_legacy_lessons=None, resources=None
):
    del config, provider, write_legacy_lessons, resources
    return None, None, _FakeCoordinator(), (lambda name: False)


def main() -> int:
    temp_root = Path(sys.argv[1]).resolve()
    os.environ["HOME"] = str(temp_root / "home")
    (temp_root / "home").mkdir(parents=True, exist_ok=True)

    sys.meta_path.insert(0, _BlockLegacy())

    from typer.testing import CliRunner

    import PhyAgentOS.cli.commands as commands
    from PhyAgentOS.cli.command_groups import agent_commands

    agent_commands.ProviderService = _FakeService
    agent_commands.build_forge_components = _fake_build_forge_components

    app = commands.app
    runner = CliRunner()

    help_result = runner.invoke(app, ["agent", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "core (default)" in help_result.output, help_result.output

    invocations = (
        ("default", ["agent", "-m", "hello"]),
        ("explicit-core", ["agent", "--runtime", "core", "-m", "hello"]),
        ("migrated-tool", ["agent", "-m", "migration-read"]),
    )
    for label, args in invocations:
        workspace = temp_root / f"workspace-{label}"
        if label == "migrated-tool":
            workspace.mkdir(parents=True)
            (workspace / "probe.txt").write_text("migration-content", encoding="utf-8")
        result = runner.invoke(app, [*args, "--workspace", str(workspace)])
        assert result.exit_code == 0, (label, result.output, result.exception)
        assert (workspace / "sessions" / "core.db").is_file(), label
        if label == "migrated-tool":
            assert "tool-read-confirmed" in result.output, result.output
    assert len(PROVIDER_CALLS) == len(invocations) + 1, PROVIDER_CALLS

    loaded = [
        name
        for name in sys.modules
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in _BLOCKED_PREFIXES
        )
    ]
    assert loaded == [], loaded
    print("probe-ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
