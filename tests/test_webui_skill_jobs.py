"""WebUI Skill action jobs and the shared install/remove service."""

from __future__ import annotations

import hashlib
import json
import tarfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from PhyAgentOS.skill_runtime.installer import InstallerError
from PhyAgentOS.skill_runtime.registry import RegistryClient
from PhyAgentOS.skill_runtime.runtime_manifest import normalize_arch, normalize_platform
from PhyAgentOS.skill_runtime.service import (
    InstallOutcome,
    SkillServicePaths,
    install_skill_from_registry,
    remove_skill,
)
from PhyAgentOS.skill_runtime.state import RuntimeState, RuntimeStateStore
from PhyAgentOS.webui.app import create_app
from PhyAgentOS.webui.skill_jobs import (
    SkillJobBusyError,
    SkillJobError,
    SkillJobManager,
)


def _build_skill_archive(tmp_path: Path, name: str, version: str, *, node: dict | None = None) -> Path:
    bundle = tmp_path / f"_{name}-{version}-build" / name
    (bundle / "profiles" / "default").mkdir(parents=True)
    (bundle / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    (bundle / "profiles" / "default" / "dataflow.yaml").write_text("nodes: []\n", encoding="utf-8")
    manifest: dict = {
        "manifest_version": 2,
        "name": name,
        "version": version,
        "description": f"{name} demo",
        "skill_document": "SKILL.md",
        "gateway_url": "http://127.0.0.1:19002",
        "required_tools": ["tool.one"],
        "profiles": {"default": {"dataflow": "profiles/default/dataflow.yaml"}},
    }
    if node is not None:
        manifest["artifacts"] = {"resolver": "registry", "nodes": {node["node_id"]: node["lock"]}}
    (bundle / "skill.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    files = []
    for file_path in sorted(bundle.rglob("*")):
        if file_path.is_file():
            files.append(
                {
                    "path": file_path.relative_to(bundle).as_posix(),
                    "sha256": hashlib.sha256(file_path.read_bytes()).hexdigest(),
                    "size": file_path.stat().st_size,
                }
            )
    manifest_file = tmp_path / f"_{name}-{version}-archive-manifest.json"
    manifest_file.write_text(json.dumps({"files": files}, indent=2), encoding="utf-8")
    archive = tmp_path / f"{name}-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for file_path in sorted(bundle.rglob("*")):
            if file_path.is_file():
                tar.add(file_path, arcname=file_path.relative_to(bundle).as_posix())
        tar.add(manifest_file, arcname="archive-manifest.json")
    return archive


def _build_node_archive(tmp_path: Path, node_id: str) -> Path:
    source = tmp_path / f"_{node_id}-src"
    source.write_bytes(b"#!/bin/sh\n")
    archive = tmp_path / f"{node_id}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname=node_id)
    return archive


class _FakeRegistryServer:
    """Mutable in-process registry: metadata + archive bytes over MockTransport."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.current: dict[str, str] = {}
        self.archives: dict[str, Path] = {}
        self.node_archives: dict[str, Path] = {}
        self.locks: dict[str, dict] = {}
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def add_skill(self, name: str, version: str, *, node: dict | None = None) -> None:
        archive = _build_skill_archive(self.tmp_path, name, version, node=node)
        self.archives[f"{name}-{version}.tar.gz"] = archive
        self.current.setdefault(name, version)
        if node is not None:
            artifact_id = node["lock"]["artifact_id"]
            self.node_archives[f"{artifact_id}.tar.gz"] = node["archive"]
            self.locks[artifact_id] = node["lock"]

    def set_current(self, name: str, version: str) -> None:
        self.current[name] = version

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/skills/"):
            name = path.rsplit("/", 1)[-1]
            version = self.current.get(name)
            archive = self.archives.get(f"{name}-{version}.tar.gz")
            if archive is None:
                return httpx.Response(404, json={"detail": "missing"})
            payload = {
                "artifact": {
                    "download_url": f"http://test/{name}-{version}.tar.gz",
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "size": archive.stat().st_size,
                },
                "name": name,
                "version": version,
            }
            return httpx.Response(200, json=payload)
        if path.startswith("/v1/forge-nodes/"):
            artifact_id = path.rsplit("/", 1)[-1]
            archive = self.node_archives.get(f"{artifact_id}.tar.gz")
            if archive is None:
                return httpx.Response(404, json={"detail": "missing"})
            lock = self.locks[artifact_id]
            payload = {
                "artifact": {
                    "download_url": f"http://test/{artifact_id}.tar.gz",
                    "sha256": lock["sha256"],
                    "size": archive.stat().st_size,
                }
            }
            return httpx.Response(200, json=payload)
        file_name = path.lstrip("/")
        archive = self.archives.get(file_name) or self.node_archives.get(file_name)
        if archive is None:
            return httpx.Response(404, json={"detail": "missing"})
        return httpx.Response(200, content=archive.read_bytes())


def _paths(tmp_path: Path) -> SkillServicePaths:
    return SkillServicePaths(
        bundles=tmp_path / "skills",
        state=tmp_path / "run" / "skills",
        forge_runtime=tmp_path / "forge_runtime",
        cache=tmp_path / "cache",
    )


def _node_lock(node_id: str, artifact_id: str, archive: Path) -> dict:
    return {
        "artifact_id": artifact_id,
        "version": "1.0.0",
        "platform": normalize_platform(),
        "arch": normalize_arch(),
        "artifact_type": "executable_tar_gz",
        "entrypoint": node_id,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }


def _wait_job(manager: SkillJobManager, job_id: str, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.snapshot_for(job_id)
        assert job is not None
        if job.status != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"resource_registry": {"url": "http://test"}}), encoding="utf-8"
    )
    return config_path


def test_service_install_update_remove(tmp_path: Path) -> None:
    server = _FakeRegistryServer(tmp_path)
    server.add_skill("demo-skill", "1.0.0")
    server.add_skill("demo-skill", "2.0.0")
    paths = _paths(tmp_path)
    registry = RegistryClient(base_url="http://test", client=server.client)

    outcome = install_skill_from_registry(
        "demo-skill", paths=paths, registry=registry, download_client=server.client
    )
    assert isinstance(outcome, InstallOutcome)
    assert outcome.status == "installed"
    assert outcome.manifest is not None and outcome.manifest.version == "1.0.0"
    assert (paths.bundles / "demo-skill" / "SKILL.md").is_file()

    again = install_skill_from_registry(
        "demo-skill", paths=paths, registry=registry, download_client=server.client
    )
    assert again.status == "already_ready"

    server.set_current("demo-skill", "2.0.0")
    updated = install_skill_from_registry(
        "demo-skill", version="2.0.0", paths=paths, registry=registry, download_client=server.client
    )
    assert updated.status == "installed"
    assert updated.manifest is not None and updated.manifest.version == "2.0.0"

    remove_skill("demo-skill", paths=paths)
    assert not (paths.bundles / "demo-skill").exists()
    with pytest.raises(InstallerError):
        remove_skill("demo-skill", paths=paths)


def test_service_installs_missing_locked_node(tmp_path: Path) -> None:
    node_archive = _build_node_archive(tmp_path, "gateway")
    lock = _node_lock("gateway", "gw-one", node_archive)
    server = _FakeRegistryServer(tmp_path)
    server.add_skill(
        "node-skill", "1.0.0", node={"node_id": "gateway", "lock": lock, "archive": node_archive}
    )
    paths = _paths(tmp_path)
    registry = RegistryClient(base_url="http://test", client=server.client)

    outcome = install_skill_from_registry(
        "node-skill", paths=paths, registry=registry, download_client=server.client
    )
    assert outcome.status == "installed"
    assert outcome.missing_nodes == ()
    assert (paths.forge_runtime / "nodes" / "gateway" / "versions" / "gw-one").is_dir()


def test_manager_install_update_remove_jobs(tmp_path: Path) -> None:
    server = _FakeRegistryServer(tmp_path)
    server.add_skill("demo-skill", "1.0.0")
    server.add_skill("demo-skill", "2.0.0")
    paths = _paths(tmp_path)
    config_path = _write_config(tmp_path)
    manager = SkillJobManager(paths, config_path, http_client=server.client)

    job = manager.start("install", "demo-skill")
    job = _wait_job(manager, job.id)
    assert job.status == "done"
    assert job.message is not None and "v1.0.0" in job.message
    assert manager.current() is job

    server.set_current("demo-skill", "2.0.0")
    update = manager.start("update", "demo-skill")
    update = _wait_job(manager, update.id)
    assert update.status == "done"

    removal = manager.start("remove", "demo-skill")
    removal = _wait_job(manager, removal.id)
    assert removal.status == "done"
    assert not (paths.bundles / "demo-skill").exists()

    missing = manager.start("update", "demo-skill")
    missing = _wait_job(manager, missing.id)
    assert missing.status == "error"
    assert missing.error is not None and "尚未安装" in missing.error


def test_manager_blocks_active_skill(tmp_path: Path) -> None:
    server = _FakeRegistryServer(tmp_path)
    server.add_skill("demo-skill", "1.0.0")
    paths = _paths(tmp_path)
    config_path = _write_config(tmp_path)
    registry = RegistryClient(base_url="http://test", client=server.client)
    assert install_skill_from_registry(
        "demo-skill", paths=paths, registry=registry, download_client=server.client
    ).status == "installed"

    manager = SkillJobManager(paths, config_path, http_client=server.client)
    RuntimeStateStore(paths.state).save(
        RuntimeState(
            skill_name="demo-skill",
            profile="default",
            status="running",
            flow_name="flow",
            gateway_url="http://127.0.0.1:19002",
        )
    )
    job = manager.start("update", "demo-skill")
    job = _wait_job(manager, job.id)
    assert job.status == "error"
    assert job.error is not None and "正在运行" in job.error

    remove_job = manager.start("remove", "demo-skill")
    remove_job = _wait_job(manager, remove_job.id)
    assert remove_job.status == "error"
    assert remove_job.error is not None and "正在运行" in remove_job.error


def test_manager_rejects_concurrent_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakeRegistryServer(tmp_path)
    server.add_skill("demo-skill", "1.0.0")
    paths = _paths(tmp_path)
    config_path = _write_config(tmp_path)
    manager = SkillJobManager(paths, config_path, http_client=server.client)

    import PhyAgentOS.webui.skill_jobs as jobs_module

    real_install = jobs_module.install_skill_from_registry

    def slow_install(*args: object, **kwargs: object) -> InstallOutcome:
        time.sleep(0.3)
        return real_install(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(jobs_module, "install_skill_from_registry", slow_install)
    first = manager.start("install", "demo-skill")
    with pytest.raises(SkillJobBusyError):
        manager.start("install", "demo-skill")
    _wait_job(manager, first.id)


def test_manager_validates_names(tmp_path: Path) -> None:
    manager = SkillJobManager(_paths(tmp_path), _write_config(tmp_path))
    for bad in ("", "../escape", "a/b", "a\\b"):
        with pytest.raises(SkillJobError):
            manager.start("install", bad)


def test_endpoints_gate_and_shapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeJob:
        def __init__(self, job_id: str) -> None:
            self.id = job_id

        def snapshot(self) -> dict:
            return {"jobId": self.id, "kind": "install", "status": "running"}

    def fake_start(self: SkillJobManager, kind: str, name: str, version: str | None = None) -> Any:
        job = _FakeJob(f"job-{kind}-{name}")
        self._job = job
        return job

    monkeypatch.setattr(SkillJobManager, "start", fake_start)

    disabled = TestClient(create_app(tmp_path / "config.json"))
    assert disabled.get("/api/forge/runtime").json()["actionsEnabled"] is False
    denied = disabled.post(
        "/api/forge/skills/install", json={"name": "demo-skill"}
    )
    assert denied.status_code == 403
    assert "--allow-skill-actions" in denied.json()["detail"]

    enabled = TestClient(create_app(tmp_path / "config.json", allow_skill_actions=True))
    assert enabled.get("/api/forge/runtime").json()["actionsEnabled"] is True
    assert enabled.get("/api/forge/jobs/current").json()["job"] is None
    started = enabled.post("/api/forge/skills/install", json={"name": "demo-skill"})
    assert started.status_code == 202
    job_id = started.json()["job"]["jobId"]
    assert enabled.get(f"/api/forge/jobs/{job_id}").status_code == 200
    assert enabled.get("/api/forge/jobs/missing").status_code == 404
    assert (
        enabled.post("/api/forge/skills/update", json={"name": "demo-skill", "version": "2.0.0"}).status_code
        == 202
    )
    assert enabled.post("/api/forge/skills/remove", json={"name": "demo-skill"}).status_code == 202


def test_endpoints_map_job_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def busy_start(self: SkillJobManager, kind: str, name: str, version: str | None = None) -> Any:
        raise SkillJobBusyError("已有任务在进行中。")

    def invalid_start(self: SkillJobManager, kind: str, name: str, version: str | None = None) -> Any:
        raise SkillJobError("Skill 名称无效。")

    enabled = create_app(tmp_path / "config.json", allow_skill_actions=True)
    monkeypatch.setattr(SkillJobManager, "start", busy_start)
    client = TestClient(enabled)
    assert client.post("/api/forge/skills/install", json={"name": "x"}).status_code == 409

    monkeypatch.setattr(SkillJobManager, "start", invalid_start)
    client = TestClient(enabled)
    assert client.post("/api/forge/skills/install", json={"name": "x"}).status_code == 400
