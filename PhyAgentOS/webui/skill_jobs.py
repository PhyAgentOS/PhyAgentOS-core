"""Background Skill install/update/remove jobs for the local settings UI.

Jobs run one at a time in a daemon thread of the settings server process.
They only mutate the instance data directory passed in via
:class:`SkillServicePaths`; running-skill guards stay in the installers.
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PhyAgentOS.config.loader import load_config
from PhyAgentOS.skill_runtime.catalog import SkillCatalog, SkillNotFoundError
from PhyAgentOS.skill_runtime.installer import InstallerError
from PhyAgentOS.skill_runtime.registry import RegistryClient, RegistryError
from PhyAgentOS.skill_runtime.service import (
    SkillServicePaths,
    install_skill_from_registry,
    remove_skill,
)
from PhyAgentOS.skill_runtime.state import RuntimeStateStore, StateError

_ACTIVE_STATUSES = {"starting", "running", "stopping"}
_VALID_NAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


class SkillJobBusyError(RuntimeError):
    """Another Skill job is already running."""


class SkillJobError(RuntimeError):
    """A job was rejected before or during execution."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_name(name: str) -> str:
    value = (name or "").strip()
    if not value or value in {".", ".."} or len(value) > 128:
        raise SkillJobError("Skill 名称无效。")
    if any(char not in _VALID_NAME_CHARS for char in value):
        raise SkillJobError("Skill 名称只能包含字母、数字、点、下划线和连字符。")
    return value


def _validate_version(version: str | None) -> str | None:
    if version is None:
        return None
    value = version.strip()
    if not value:
        return None
    if len(value) > 64 or any(char not in _VALID_NAME_CHARS for char in value):
        raise SkillJobError("Skill 版本号无效。")
    return value


def resolve_registry_base_url(config_path: Path) -> str:
    """Resolve the registry URL the same way the CLI does (env override first)."""
    configured = os.environ.get("PAOS_RESOURCE_REGISTRY_URL", "").strip()
    if not configured:
        configured = load_config(config_path).resource_registry.url.strip()
    if not configured.startswith(("http://", "https://")):
        raise RegistryError(
            "Resource Registry URL is not configured; set PAOS_RESOURCE_REGISTRY_URL "
            "or resourceRegistry.url"
        )
    return configured.rstrip("/")


@dataclass
class SkillJob:
    """One queued or finished Skill operation."""

    id: str
    kind: str
    name: str
    version: str | None
    status: str = "running"
    step: str = "准备"
    detail: str = ""
    message: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "jobId": self.id,
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "status": self.status,
            "step": self.step,
            "detail": self.detail,
            "message": self.message,
            "error": self.error,
            "createdAt": self.created_at,
            "finishedAt": self.finished_at,
        }


class SkillJobManager:
    """Run at most one Skill job at a time for this settings server."""

    def __init__(
        self,
        paths: SkillServicePaths,
        config_path: Path,
        *,
        http_client: Any = None,
    ) -> None:
        self._paths = paths
        self._config_path = config_path
        self._http_client = http_client
        self._lock = threading.Lock()
        self._job: SkillJob | None = None

    def current(self) -> SkillJob | None:
        """Return the running or most recent job (kept until the next one)."""
        with self._lock:
            return self._job

    def snapshot_for(self, job_id: str) -> SkillJob | None:
        with self._lock:
            if self._job is not None and self._job.id == job_id:
                return self._job
        return None

    def start(self, kind: str, name: str, version: str | None = None) -> SkillJob:
        clean_name = _validate_name(name)
        clean_version = _validate_version(version)
        if kind not in {"install", "update", "remove"}:
            raise SkillJobError(f"未知任务类型 {kind!r}")
        with self._lock:
            if self._job is not None and self._job.status == "running":
                raise SkillJobBusyError(
                    f"已有任务在进行中：{self._job.kind} {self._job.name}。请等待完成。"
                )
            job = SkillJob(
                id=uuid.uuid4().hex,
                kind=kind,
                name=clean_name,
                version=clean_version,
            )
            self._job = job
        thread = threading.Thread(
            target=self._run, args=(job,), daemon=True, name=f"skill-job-{job.id}"
        )
        thread.start()
        return job

    def _run(self, job: SkillJob) -> None:
        try:
            if job.kind in {"install", "update"}:
                self._install(job)
            else:
                self._remove(job)
            if job.status == "running":
                job.status = "done"
                if job.message is None:
                    job.message = "完成"
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
            job.status = "error"
            job.error = str(exc) or exc.__class__.__name__
        finally:
            job.finished_at = _now()

    def _require_stopped(self, job: SkillJob, verb: str) -> None:
        store = RuntimeStateStore(self._paths.state)
        try:
            state = store.load(job.name)
        except StateError as exc:
            raise SkillJobError(
                f"无法读取 Skill {job.name} 的运行状态：{exc}"
            ) from None
        if state is not None and (
            state.status in _ACTIVE_STATUSES
            or state.active_invocations
            or state.active_sessions
            or state.active_task_bindings
        ):
            raise SkillJobError(
                f"Skill {job.name} 正在运行或仍有未结束的调用；请先停止再{verb}。"
            )

    def _install(self, job: SkillJob) -> None:
        verb = "更新" if job.kind == "update" else "安装"
        if job.kind == "update":
            try:
                SkillCatalog(self._paths.bundles).get(job.name)
            except SkillNotFoundError:
                raise SkillJobError(
                    f"Skill {job.name} 尚未安装；请先安装再更新。"
                ) from None
        self._require_stopped(job, verb)
        base_url = resolve_registry_base_url(self._config_path)
        job.step = "查询仓库"
        job.detail = base_url
        registry = RegistryClient(base_url=base_url, client=self._http_client)

        def progress(event: str, artifact: Any, downloaded: int, total: int | None) -> None:
            label = artifact.name or artifact.version or "artifact"
            job.step = "下载"
            if event == "cached":
                job.detail = f"使用缓存：{label}"
            elif total:
                job.detail = f"{label} {max(0, min(100, int(downloaded * 100 / total)))}%"
            else:
                job.detail = label

        outcome = install_skill_from_registry(
            job.name,
            version=job.version,
            paths=self._paths,
            confirm=None,
            progress=progress,
            registry=registry,
            download_client=self._http_client,
        )
        if outcome.status == "cancelled":
            job.status = "cancelled"
            job.message = "已取消"
            return
        manifest = outcome.manifest
        assert manifest is not None
        job.step = "完成"
        if outcome.status == "already_ready":
            job.message = f"Skill {manifest.name} 已是就绪版本 v{manifest.version}"
        else:
            job.message = f"已安装 {manifest.name} v{manifest.version}"
        if outcome.missing_nodes:
            job.message += (
                "；缺少 Node：" + ", ".join(outcome.missing_nodes)
                + "（可用 paos forge-node install 安装）"
            )

    def _remove(self, job: SkillJob) -> None:
        self._require_stopped(job, "删除")
        job.step = "删除"
        try:
            remove_skill(job.name, paths=self._paths)
        except InstallerError as exc:
            raise SkillJobError(str(exc)) from None
        job.message = f"已删除 {job.name}"
