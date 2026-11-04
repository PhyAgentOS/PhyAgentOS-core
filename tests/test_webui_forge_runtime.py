"""WebUI Forge runtime read-only endpoint: installed Skill info without actions."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from PhyAgentOS.skill_runtime.installer import NodeInstaller
from PhyAgentOS.skill_runtime.registry import RegistryError
from PhyAgentOS.skill_runtime.runtime_manifest import normalize_arch, normalize_platform
from PhyAgentOS.skill_runtime.state import RuntimeState, RuntimeStateStore
from PhyAgentOS.webui import app as app_module
from PhyAgentOS.webui import forge_runtime
from PhyAgentOS.webui.app import create_app
from PhyAgentOS.webui.forge_runtime import forge_runtime_snapshot


def _write_skill_bundle(bundles: Path, *, profile_tools: bool = False) -> None:
    bundle = bundles / "move-arm-by-ee"
    (bundle / "profiles" / "mujoco").mkdir(parents=True)
    (bundle / "SKILL.md").write_text("# Move arm\n", encoding="utf-8")
    (bundle / "profiles" / "mujoco" / "dataflow.yaml").write_text(
        "nodes: []\n", encoding="utf-8"
    )
    profile: dict = {"dataflow": "profiles/mujoco/dataflow.yaml"}
    if profile_tools:
        profile["required_tools"] = ["motion.move_pose"]
    manifest = {
        "manifest_version": 2,
        "name": "move-arm-by-ee",
        "version": "1.0.0",
        "description": "Move an arm.",
        "skill_document": "SKILL.md",
        "gateway_url": "http://127.0.0.1:19002",
        "required_tools": ["motion.resolve_relative_pose", "motion.move_pose"],
        "profiles": {"mujoco": profile},
    }
    (bundle / "skill.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )


def _write_node_lock(
    manifest: dict,
    *,
    node_id: str,
    artifact_id: str,
    archive: Path,
) -> None:
    manifest.setdefault("artifacts", {"resolver": "registry", "nodes": {}})
    manifest["artifacts"]["nodes"][node_id] = {
        "artifact_id": artifact_id,
        "version": "1.0.0",
        "platform": normalize_platform(),
        "arch": normalize_arch(),
        "artifact_type": "executable_tar_gz",
        "entrypoint": node_id,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }


def _build_node_archive(tmp_path: Path, name: str) -> Path:
    source = tmp_path / name
    source.write_bytes(b"#!/bin/sh\n")
    archive = tmp_path / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle_archive:
        bundle_archive.add(source, arcname=name)
    return archive


def _snapshot(tmp_path: Path) -> dict:
    return forge_runtime_snapshot(tmp_path)


def test_empty_data_dir_reports_no_skills(tmp_path: Path) -> None:
    data = _snapshot(tmp_path)
    assert data == {"skills": [], "invalid": []}


def test_snapshot_reports_not_started_skill(tmp_path: Path) -> None:
    _write_skill_bundle(tmp_path / "skills")
    data = _snapshot(tmp_path)
    assert data["invalid"] == []
    assert len(data["skills"]) == 1
    skill = data["skills"][0]
    assert skill["name"] == "move-arm-by-ee"
    assert skill["version"] == "1.0.0"
    assert skill["status"] == "not_started"
    assert skill["profile"] is None
    assert skill["gatewayUrl"] == "http://127.0.0.1:19002"
    assert skill["tools"] == ["motion.resolve_relative_pose", "motion.move_pose"]
    assert skill["nodes"] == []


def test_snapshot_prefers_runtime_state_and_profile_tools(tmp_path: Path) -> None:
    _write_skill_bundle(tmp_path / "skills", profile_tools=True)
    states = RuntimeStateStore(tmp_path / "run" / "skills")
    states.save(
        RuntimeState(
            skill_name="move-arm-by-ee",
            profile="mujoco",
            status="running",
            flow_name="move-flow",
            gateway_url="http://127.0.0.2:19003",
            active_sessions=("s1",),
            active_task_bindings=("t1", "t2"),
        )
    )
    skill = _snapshot(tmp_path)["skills"][0]
    assert skill["status"] == "running"
    assert skill["profile"] == "mujoco"
    assert skill["gatewayUrl"] == "http://127.0.0.2:19003"
    assert skill["tools"] == ["motion.move_pose"]
    assert skill["activeSessions"] == 1
    assert skill["activeTaskBindings"] == 2
    assert skill["startedAt"] is None or isinstance(skill["startedAt"], str)


def test_snapshot_reports_node_install_status_without_hashing(tmp_path: Path) -> None:
    bundles = tmp_path / "skills"
    _write_skill_bundle(bundles)
    manifest = yaml.safe_load(
        (bundles / "move-arm-by-ee" / "skill.yaml").read_text(encoding="utf-8")
    )
    gateway_archive = _build_node_archive(tmp_path, "gateway")
    _write_node_lock(manifest, node_id="gateway", artifact_id="gw-one", archive=gateway_archive)
    vision_archive = _build_node_archive(tmp_path, "vision")
    _write_node_lock(manifest, node_id="vision", artifact_id="vs-one", archive=vision_archive)
    (bundles / "move-arm-by-ee" / "skill.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )

    states = RuntimeStateStore(tmp_path / "run" / "skills")
    installer = NodeInstaller(tmp_path / "forge_runtime", state_store=states)
    lock = manifest["artifacts"]["nodes"]["gateway"]
    from PhyAgentOS.skill_runtime.manifest import NodeLock

    installer.install(gateway_archive, NodeLock.from_dict("gateway", lock))

    nodes = {node["nodeId"]: node for node in _snapshot(tmp_path)["skills"][0]["nodes"]}
    assert nodes["gateway"]["installed"] is True
    assert nodes["gateway"]["version"] == "1.0.0"
    assert nodes["vision"]["installed"] is False

    # A light check stays False when the receipt is tampered with.
    receipt_path = (
        tmp_path / "forge_runtime" / "nodes" / "gateway" / "versions" / "gw-one" / ".paos-node.json"
    )
    receipt = yaml.safe_load(receipt_path.read_text(encoding="utf-8"))
    receipt["archive_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert (
        NodeInstaller(tmp_path / "forge_runtime").installed_light(
            NodeLock.from_dict("gateway", lock)
        )
        is False
    )


def test_snapshot_lists_invalid_bundles(tmp_path: Path) -> None:
    broken = tmp_path / "skills" / "broken-skill"
    broken.mkdir(parents=True)
    (broken / "skill.yaml").write_text(
        yaml.safe_dump({"manifest_version": 1, "name": "broken-skill"}), encoding="utf-8"
    )
    data = _snapshot(tmp_path)
    assert data["skills"] == []
    assert data["invalid"] and data["invalid"][0]["name"] == "broken-skill"
    assert data["invalid"][0]["error"]


def test_endpoint_returns_runtime_snapshot(tmp_path: Path) -> None:
    _write_skill_bundle(tmp_path / "skills")
    client = TestClient(create_app(tmp_path / "config.json"))
    response = client.get("/api/forge/runtime")
    assert response.status_code == 200
    payload = response.json()
    assert [skill["name"] for skill in payload["skills"]] == ["move-arm-by-ee"]
    assert payload["invalid"] == []


def test_endpoint_respects_token(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "config.json", token="t0ken"))
    assert client.get("/api/forge/runtime").status_code == 401
    assert (
        client.get("/api/forge/runtime", headers={"x-paos-token": "t0ken"}).status_code
        == 200
    )


class _FakeRegistry:
    """Context-manager registry client stub returning canned search results."""

    calls: list[str] = []

    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def __enter__(self) -> "_FakeRegistry":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def search_skills(self, query: str = "") -> list[dict]:
        _FakeRegistry.calls.append(query)
        return self._items


def _registry_config(tmp_path: Path, *, url: str) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"resource_registry": {"url": url}}), encoding="utf-8"
    )
    return config_path


def test_registry_search_merges_local_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PAOS_RESOURCE_REGISTRY_URL", raising=False)
    _write_skill_bundle(tmp_path / "skills")
    items = [
        {"name": "move-arm-by-ee", "version": "1.0.0", "description": "same"},
        {"name": "move-arm-by-ee-old", "version": "9.9.9", "description": "older"},
    ]
    # A second entry with the same installed name but a newer version:
    items.insert(1, {"name": "move-arm-by-ee", "version": "2.0.0", "description": "new"})
    del items[0]

    seen_urls: list[str] = []

    def factory(url: str):
        seen_urls.append(url)
        return _FakeRegistry(items)

    data = forge_runtime.registry_search(
        tmp_path, _registry_config(tmp_path, url="https://registry.example.com"), "", client_factory=factory
    )
    assert seen_urls == ["https://registry.example.com"]
    assert data["sourceUrl"] == "https://registry.example.com"
    by_name = {item["name"]: item for item in data["items"]}
    assert by_name["move-arm-by-ee"]["installedVersion"] == "1.0.0"
    assert by_name["move-arm-by-ee"]["updateAvailable"] is True
    assert by_name["move-arm-by-ee-old"]["installedVersion"] is None
    assert by_name["move-arm-by-ee-old"]["updateAvailable"] is False


def test_registry_search_env_overrides_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAOS_RESOURCE_REGISTRY_URL", "https://env.example.com/")
    seen_urls: list[str] = []

    def factory(url: str):
        seen_urls.append(url)
        return _FakeRegistry([])

    data = forge_runtime.registry_search(
        tmp_path, _registry_config(tmp_path, url="https://registry.example.com"), "", client_factory=factory
    )
    assert seen_urls == ["https://env.example.com"]
    assert data["sourceUrl"] == "https://env.example.com"


def test_registry_search_requires_configured_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PAOS_RESOURCE_REGISTRY_URL", raising=False)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"resource_registry": {"url": ""}}), encoding="utf-8"
    )
    with pytest.raises(RegistryError):
        forge_runtime.registry_search(tmp_path, config_path, "", client_factory=_FakeRegistry)


def test_registry_endpoint_returns_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PAOS_RESOURCE_REGISTRY_URL", raising=False)
    _write_skill_bundle(tmp_path / "skills")
    items = [{"name": "move-arm-by-ee", "version": "2.0.0", "description": "new"}]

    def fake_search(data_dir: Path, config_path: Path, query: str = "", **_: object) -> dict:
        assert query == "move"
        return forge_runtime.registry_search(
            data_dir, config_path, query, client_factory=lambda url: _FakeRegistry(items)
        )

    monkeypatch.setattr(app_module, "registry_search", fake_search)
    client = TestClient(create_app(tmp_path / "config.json"))
    response = client.get("/api/forge/registry", params={"query": "move"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["updateAvailable"] is True


def test_registry_endpoint_maps_registry_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_search(*_: object, **__: object) -> dict:
        raise RegistryError("Resource Registry request failed: /v1/skills")

    monkeypatch.setattr(app_module, "registry_search", fake_search)
    client = TestClient(create_app(tmp_path / "config.json"))
    response = client.get("/api/forge/registry")
    assert response.status_code == 400
    assert "request failed" in response.json()["detail"]
