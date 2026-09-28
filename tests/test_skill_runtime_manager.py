from __future__ import annotations

import hashlib
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))

from PhyAgentOS.skill_runtime.catalog import SkillCatalog  # noqa: E402
from PhyAgentOS.skill_runtime.installer import NodeInstaller  # noqa: E402
from PhyAgentOS.skill_runtime.manager import RuntimeManager, RuntimeManagerError  # noqa: E402
from PhyAgentOS.skill_runtime.runtime_manifest import (  # noqa: E402
    normalize_arch,
    normalize_platform,
)
from PhyAgentOS.skill_runtime.state import RuntimeState, RuntimeStateStore  # noqa: E402


def _setup(tmp_path: Path) -> tuple[SkillCatalog, Path, Path, RuntimeStateStore]:
    bundles = tmp_path / "bundles"
    bundle = bundles / "move-arm-by-ee"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text("# Move arm\n", encoding="utf-8")
    profile = bundle / "profiles" / "mujoco"
    (bundle / "assets").mkdir()
    profile.mkdir(parents=True)
    (profile / "dataflow.yaml").write_text(
        "nodes:\n  - id: gateway\n    path: ${FORGE_RUNTIME_BIN}/gateway\n",
        encoding="utf-8",
    )
    (bundle / "assets" / "scene.xml").write_text("<mujoco/>\n", encoding="utf-8")
    marker = b"#!/bin/sh\n"
    source = tmp_path / "gateway"
    source.write_bytes(marker)
    archive = tmp_path / "gateway.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle_archive:
        bundle_archive.add(source, arcname="gateway")
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {
        "manifest_version": 2,
        "name": "move-arm-by-ee",
        "version": "1.0.0",
        "description": "Move an arm.",
        "skill_document": "SKILL.md",
        "gateway_url": "http://127.0.0.1:19002",
        "required_tools": ["motion.resolve_relative_pose", "motion.move_pose"],
        "profiles": {
            "mujoco": {
                "dataflow": "profiles/mujoco/dataflow.yaml",
                "required_binaries": ["gateway"],
                "required_assets": ["assets/scene.xml"],
            }
        },
        "artifacts": {
            "resolver": "registry",
            "nodes": {
                "gateway": {
                    "artifact_id": "gateway-one",
                    "version": "1.0.0",
                    "platform": normalize_platform(),
                    "arch": normalize_arch(),
                    "artifact_type": "executable_tar_gz",
                    "entrypoint": "gateway",
                    "sha256": archive_sha256,
                }
            },
        },
    }
    (bundle / "skill.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )

    runtime = tmp_path / "runtime"
    states = RuntimeStateStore(tmp_path / "state")
    catalog = SkillCatalog(bundles)
    NodeInstaller(runtime, state_store=states).install(
        archive, catalog.get("move-arm-by-ee").artifacts.nodes["gateway"]
    )
    return catalog, runtime, tmp_path / "logs", states


def _manager(tmp_path: Path) -> RuntimeManager:
    catalog, runtime, logs, states = _setup(tmp_path)
    return RuntimeManager(
        catalog=catalog,
        state_store=states,
        runtime_root=runtime,
        logs_root=logs,
        health_timeout_s=0.1,
        poll_interval_s=0,
    )


def test_start_uses_named_attached_launcher_for_relative_mujoco_dataflow(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    commands: list[tuple[list[str], Path | None]] = []
    launched: list[tuple[list[str], Path | None, dict[str, str] | None]] = []

    def fake_run(command, *, cwd=None, env=None, timeout):
        commands.append((command, cwd))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    snapshots = iter([None, {"ok": True}, {"ok": True}])

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(command, *, cwd=None, env=None, **kwargs):
        launched.append((command, cwd, env))
        return FakeProcess()

    monkeypatch.setattr("shutil.which", lambda name: "dora")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr(manager, "_gateway_snapshot", lambda manifest: next(snapshots))
    monkeypatch.setattr(manager, "_flow_running", lambda flow_name: True)
    monkeypatch.setattr(
        manager,
        "_tool_context_readiness",
        lambda manifest: {tool_id: True for tool_id in manifest.required_tools},
    )

    state = manager.start("move-arm-by-ee", "mujoco")

    assert state.status == "running"
    start_command, cwd, env = launched[0]
    assert start_command[1:] == [
        "start",
        "--name",
        "paos-move-arm-by-ee-mujoco",
        "dataflow.yaml",
    ]
    assert cwd == manager.runtime_root.parent / "launch" / "profiles" / "mujoco"
    assert env is not None
    assert env["FORGE_RUNTIME_BIN"] == str(manager.runtime_root)
    assert (manager.runtime_root / "gateway").is_file()
    assert env["PAOS_SKILL_ROOT"] == str(manager.catalog.root / "move-arm-by-ee")
    rendered = (cwd / "dataflow.yaml").read_text()
    assert "${FORGE_RUNTIME_BIN}" not in rendered
    assert f"path: {manager.runtime_root}/gateway" in rendered


def test_start_is_idempotent_when_live_runtime_is_ready(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    state = RuntimeState(
        skill_name="move-arm-by-ee",
        profile="mujoco",
        status="running",
        flow_name="paos-move-arm-by-ee-mujoco",
        gateway_url="http://127.0.0.1:19002",
    )
    manager.state_store.save(state)
    monkeypatch.setattr(manager, "_flow_running", lambda flow_name: True)
    monkeypatch.setattr(manager, "_gateway_snapshot", lambda manifest: {"ok": True})
    monkeypatch.setattr(
        manager,
        "_tool_context_readiness",
        lambda manifest: {tool_id: True for tool_id in manifest.required_tools},
    )
    monkeypatch.setattr(manager, "_start_flow", lambda *args: pytest.fail("started twice"))

    assert manager.start("move-arm-by-ee", "mujoco").status == "running"


def test_status_marks_stale_running_state_failed(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.state_store.save(
        RuntimeState(
            skill_name="move-arm-by-ee",
            profile="mujoco",
            status="running",
            flow_name="paos-move-arm-by-ee-mujoco",
            gateway_url="http://127.0.0.1:19002",
        )
    )
    monkeypatch.setattr(manager, "_flow_running", lambda flow_name: False)
    monkeypatch.setattr(manager, "_gateway_snapshot", lambda manifest: None)

    report = manager.status("move-arm-by-ee")

    assert report.state is not None
    assert report.state.status == "failed"
    assert "Dora flow is not running" in (report.state.last_error or "")


def test_start_failure_rolls_back_flow_and_persists_diagnostic(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    stopped: list[tuple[str, bool, bool]] = []
    monkeypatch.setattr("shutil.which", lambda name: "dora")
    monkeypatch.setattr(
        manager,
        "_run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(manager, "_gateway_snapshot", lambda manifest: None)
    monkeypatch.setattr(
        manager,
        "_start_flow",
        lambda *args: (_ for _ in ()).throw(RuntimeManagerError("dora start failed")),
    )
    monkeypatch.setattr(
        manager,
        "_stop_flow",
        lambda flow, *, force, check: stopped.append((flow, force, check)),
    )

    with pytest.raises(RuntimeManagerError, match="dora start failed"):
        manager.start("move-arm-by-ee", "mujoco")

    state = manager.state_store.load("move-arm-by-ee")
    assert state is not None
    assert state.status == "failed"
    assert state.last_error == "dora start failed"
    assert stopped == [("paos-move-arm-by-ee-mujoco", True, False)]


def test_stop_refuses_nonterminal_invocations_without_force(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.state_store.save(
        RuntimeState(
            skill_name="move-arm-by-ee",
            profile="mujoco",
            status="running",
            flow_name="paos-move-arm-by-ee-mujoco",
            gateway_url="http://127.0.0.1:19002",
            active_invocations=("invocation-1",),
        )
    )

    with pytest.raises(RuntimeManagerError, match="non-terminal"):
        manager.stop("move-arm-by-ee")


def test_start_rejects_node_that_does_not_match_lock(tmp_path: Path) -> None:
    catalog, runtime, logs, states = _setup(tmp_path)
    bundle = catalog.root / "move-arm-by-ee"
    skill = yaml.safe_load((bundle / "skill.yaml").read_text())
    skill["artifacts"]["nodes"]["gateway"]["sha256"] = "0" * 64
    (bundle / "skill.yaml").write_text(yaml.safe_dump(skill))
    manager = RuntimeManager(
        catalog=catalog,
        state_store=states,
        runtime_root=runtime,
        logs_root=logs,
    )

    with pytest.raises(RuntimeManagerError, match="receipt does not match Skill lock"):
        manager.start("move-arm-by-ee", "mujoco")
