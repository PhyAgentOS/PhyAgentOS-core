"""Explicit lifecycle manager for local Forge Skill runtimes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from PhyAgentOS.config.paths import (
    get_forge_runtime_root,
    get_skill_runtime_logs_dir,
)
from PhyAgentOS.skill_runtime.catalog import SkillCatalog
from PhyAgentOS.skill_runtime.installer import InstallerError, SkillEnvironmentBuilder
from PhyAgentOS.skill_runtime.locking import SkillOperationBusyError, SkillOperationLock
from PhyAgentOS.skill_runtime.manifest import (
    ManifestError,
    RuntimeProfile,
    SkillManifest,
)
from PhyAgentOS.skill_runtime.state import RuntimeState, RuntimeStateStore, utc_now


class RuntimeManagerError(RuntimeError):
    """Raised when a managed Skill lifecycle operation cannot complete."""


@dataclass(frozen=True)
class RuntimeStatusReport:
    """Persisted and live status reconciled at query time."""

    state: RuntimeState | None
    flow_running: bool
    gateway_ready: bool
    tool_contexts: dict[str, bool]

    @property
    def ready(self) -> bool:
        return (
            self.state is not None
            and self.state.status == "running"
            and self.flow_running
            and self.gateway_ready
            and all(self.tool_contexts.values())
        )


class RuntimeManager:
    """Start and stop named Dora dataflows for installed Skill bundles."""

    def __init__(
        self,
        *,
        catalog: SkillCatalog | None = None,
        state_store: RuntimeStateStore | None = None,
        runtime_root: Path | None = None,
        logs_root: Path | None = None,
        health_timeout_s: float = 30.0,
        poll_interval_s: float = 0.25,
    ) -> None:
        self.catalog = catalog or SkillCatalog()
        self.state_store = state_store or RuntimeStateStore()
        self.installation_root = (runtime_root or get_forge_runtime_root()).expanduser().resolve()
        self.runtime_root = self.installation_root
        self.environment_builder = SkillEnvironmentBuilder(
            self.installation_root,
            state_store=self.state_store,
        )
        self.logs_root = (logs_root or get_skill_runtime_logs_dir()).expanduser()
        self.health_timeout_s = health_timeout_s
        self.poll_interval_s = poll_interval_s

    @staticmethod
    def flow_name(skill_name: str, profile: str) -> str:
        safe = f"paos-{skill_name}-{profile}"
        if not safe.replace("-", "").replace("_", "").isalnum():
            raise RuntimeManagerError("Skill and profile names must be Dora-name safe")
        return safe

    def start(self, skill_name: str, profile_name: str) -> RuntimeState:
        try:
            with SkillOperationLock(self.state_store.root, skill_name):
                return self._start_locked(skill_name, profile_name)
        except (SkillOperationBusyError, ValueError) as exc:
            raise RuntimeManagerError(str(exc)) from exc

    def _start_locked(self, skill_name: str, profile_name: str) -> RuntimeState:
        manifest = self.catalog.get(skill_name)
        profile = manifest.profiles.get(profile_name)
        if profile is None:
            available = ", ".join(sorted(manifest.profiles))
            raise RuntimeManagerError(
                f"Unknown profile {profile_name!r}; available profiles: {available}"
            )
        flow_name = self.flow_name(skill_name, profile_name)
        previous = self.state_store.load(skill_name)
        if (
            previous is not None
            and previous.profile == profile_name
            and previous.status in {"starting", "running"}
        ):
            report = self.status(skill_name)
            if report.ready:
                return report.state  # type: ignore[return-value]

        try:
            binary_root = self.environment_builder.prepare(manifest, profile_name)
        except InstallerError as exc:
            raise RuntimeManagerError(str(exc)) from exc
        self.runtime_root = binary_root
        self._preflight(manifest, profile, binary_root)
        if self._gateway_snapshot(manifest) is not None:
            raise RuntimeManagerError(
                f"Gateway address {manifest.gateway_url} is already in use; "
                "refusing to adopt an unmanaged runtime"
            )

        starting = RuntimeState(
            skill_name=skill_name,
            profile=profile_name,
            status="starting",
            flow_name=flow_name,
            gateway_url=manifest.gateway_url,
            started_at=utc_now(),
        )
        self.state_store.save(starting)
        self._log(skill_name, f"starting profile={profile_name} flow={flow_name}")
        launched = False
        try:
            self._run_start_hook(manifest, profile_name)
            self._ensure_dora_up(manifest, profile, binary_root)
            launched = True
            self._start_flow(flow_name, manifest, profile, binary_root)
            self._wait_until_ready(
                manifest,
                flow_name,
                tools=manifest.tools_for_profile(profile_name),
                timeout_s=self._startup_timeout_s(profile),
            )
            snapshot = self._gateway_snapshot(manifest) or {}
            data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else {}
            identity = data.get("gateway_identity") or data.get("gateway_id")
            if not isinstance(identity, str) or not identity:
                identity = "gateway_" + hashlib.sha256(
                    json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()[:20]
            starting = replace(starting, gateway_identity=identity)
            running = starting.with_status("running")
            self.state_store.save(running)
            self._log(skill_name, "runtime ready")
            return running
        except Exception as exc:
            message = self._safe_error(exc)
            if launched:
                self._stop_flow(flow_name, force=True, check=False)
            failed = starting.with_status("failed", error=message)
            self.state_store.save(failed)
            self._log(skill_name, f"startup failed: {message}")
            if isinstance(exc, RuntimeManagerError):
                raise
            raise RuntimeManagerError(message) from exc

    def status(self, skill_name: str) -> RuntimeStatusReport:
        manifest = self.catalog.get(skill_name)
        state = self.state_store.load(skill_name)
        if state is None:
            return RuntimeStatusReport(None, False, False, {})
        startup_in_progress = False
        if state.status == "starting":
            try:
                with SkillOperationLock(self.state_store.root, skill_name):
                    pass
            except SkillOperationBusyError:
                startup_in_progress = True
        flow_running = self._flow_running(state.flow_name)
        try:
            tools = manifest.tools_for_profile(state.profile)
        except ManifestError as exc:
            # The installed bundle no longer declares the profile this Runtime
            # was started with (the Skill was replaced underneath it). Readiness
            # cannot be evaluated against a Tool list that is not that profile's,
            # so report the mismatch instead of a readiness verdict.
            if state.status in {"stopped", "failed"}:
                return RuntimeStatusReport(state, flow_running, False, {})
            reconciled = state.with_status("failed", error=str(exc))
            self.state_store.save(reconciled)
            return RuntimeStatusReport(reconciled, flow_running, False, {})
        snapshot = self._gateway_snapshot(manifest)
        contexts = (
            self._tool_context_readiness(manifest, tools) if snapshot is not None else {}
        )
        gateway_ready = snapshot is not None
        live_ready = flow_running and gateway_ready and all(
            contexts.get(tool_id, False) for tool_id in tools
        )
        reconciled = state
        if live_ready and state.status in {"starting", "running", "failed"}:
            reconciled = state.with_status("running")
        elif state.status == "stopping" and not flow_running:
            reconciled = state.with_status(
                "stopped", active_invocations=(), active_sessions=(), active_task_bindings=()
            )
        elif (
            state.status in {"starting", "running"}
            and not live_ready
            and not startup_in_progress
        ):
            reasons = []
            if not flow_running:
                reasons.append("Dora flow is not running")
            if not gateway_ready:
                reasons.append("Gateway GET /tools is unavailable")
            missing = [tool for tool in tools if not contexts.get(tool, False)]
            if missing and gateway_ready:
                reasons.append(f"Tool context is not ready: {', '.join(missing)}")
            reconciled = state.with_status("failed", error="; ".join(reasons))
        if reconciled != state:
            self.state_store.save(reconciled)
        return RuntimeStatusReport(reconciled, flow_running, gateway_ready, contexts)

    def stop(self, skill_name: str, *, force: bool = False) -> RuntimeState:
        try:
            with SkillOperationLock(self.state_store.root, skill_name):
                return self._stop_locked(skill_name, force=force)
        except (SkillOperationBusyError, ValueError) as exc:
            raise RuntimeManagerError(str(exc)) from exc

    def _stop_locked(self, skill_name: str, *, force: bool) -> RuntimeState:
        manifest = self.catalog.get(skill_name)
        state = self.state_store.load(skill_name)
        if state is None:
            raise RuntimeManagerError(f"Skill {skill_name!r} has no runtime state")
        if state.status == "stopped" and not self._flow_running(state.flow_name):
            return state
        active_refs = (
            list(state.active_invocations)
            + list(state.active_sessions)
            + list(state.active_task_bindings)
        )
        if active_refs and not force:
            raise RuntimeManagerError(
                "Runtime has non-terminal invocation/session/task binding(s); reconcile them "
                "before stopping, or pass --force"
            )
        audit_events = state.audit_events
        if force and active_refs:
            control_attempts = [
                self._request_gateway_control(
                    f"{manifest.gateway_url}/invocations/"
                    f"{quote(invocation_id, safe='')}/cancel",
                    reference=invocation_id,
                    operation="cancel",
                )
                for invocation_id in state.active_invocations
            ]
            control_attempts.extend(
                self._request_gateway_control(
                    f"{manifest.gateway_url}/invocations/"
                    f"{quote(session_id, safe='')}/stop",
                    reference=session_id,
                    operation="stop",
                )
                for session_id in state.active_sessions
            )
            audit_events = (*audit_events, {
                "at": utc_now(),
                "event": "force_stop_with_active_references",
                "references": sorted(active_refs),
                "control_attempts": control_attempts,
            })
        stopping = state.with_status("stopping", audit_events=audit_events)
        self.state_store.save(stopping)
        self._log(skill_name, f"stopping flow={state.flow_name} force={force}")
        try:
            self._stop_flow(state.flow_name, force=force)
        except Exception as exc:
            message = self._safe_error(exc)
            failed = stopping.with_status("failed", error=message)
            self.state_store.save(failed)
            self._log(skill_name, f"stop failed: {message}")
            if isinstance(exc, RuntimeManagerError):
                raise
            raise RuntimeManagerError(message) from exc
        stopped = stopping.with_status(
            "stopped", active_invocations=(), active_sessions=(), active_task_bindings=()
        )
        self.state_store.save(stopped)
        self._log(skill_name, "runtime stopped")
        return stopped

    def read_logs(self, skill_name: str, *, lines: int = 200) -> str:
        self.catalog.get(skill_name)
        if lines < 1:
            raise RuntimeManagerError("lines must be positive")
        path = self.logs_root / f"{skill_name}.log"
        state = self.state_store.load(skill_name)
        launch_path = (
            None
            if state is None
            else self.logs_root / f"{state.flow_name}-dora.log"
        )
        sections = []
        if path.is_file():
            sections.append(path.read_text(encoding="utf-8"))
        if launch_path is not None and launch_path.is_file():
            sections.append(launch_path.read_text(encoding="utf-8", errors="replace"))
        combined = "".join(sections)
        return "".join(combined.splitlines(keepends=True)[-lines:])

    def _preflight(
        self, skill: SkillManifest, profile: RuntimeProfile, binary_root: Path
    ) -> None:
        dora = shutil.which("dora")
        if dora is None:
            raise RuntimeManagerError("dora is not installed or not available on PATH")
        result = self._run([dora, "--version"], timeout=5)
        if result.returncode != 0:
            raise RuntimeManagerError("dora version check failed")
        self._skill_path(skill, profile.dataflow, kind="dataflow")
        for relative in profile.required_binaries:
            self._binary_path(binary_root, relative)
        for relative in profile.required_assets:
            self._skill_path(skill, relative, kind="required asset")
        missing_environment = [
            name for name in profile.required_environment if not os.environ.get(name)
        ]
        if missing_environment:
            raise RuntimeManagerError(
                f"Required environment is not configured: {', '.join(missing_environment)}"
            )

    def _skill_path(self, skill: SkillManifest, relative: Path, *, kind: str) -> Path:
        candidate = skill.resolve_bundle_path(relative)
        if not candidate.is_file():
            raise RuntimeManagerError(f"{kind} is missing from Skill Bundle: {relative.as_posix()}")
        return candidate

    def _binary_path(self, binary_root: Path, relative: Path) -> Path:
        candidate = binary_root / relative
        if not candidate.is_file():
            raise RuntimeManagerError(f"required binary is missing: {relative.as_posix()}")
        if not os.access(candidate, os.X_OK):
            raise RuntimeManagerError(f"required binary is not executable: {relative.as_posix()}")
        return candidate

    @staticmethod
    def _launch_dataflow(binary_root: Path, profile: RuntimeProfile) -> Path:
        path = binary_root.parent / "launch" / profile.dataflow
        if not path.is_file():
            raise RuntimeManagerError("rendered Skill dataflow is missing")
        return path

    def _run_start_hook(self, skill: SkillManifest, profile_name: str) -> None:
        """Run the PR98 bundle ``start.sh`` hook before launching Dora.

        The hook selects profile-specific dependencies (weights, tokenizer,
        downloads). It receives the bundle identity plus the active profile name
        through the environment, so a single bundle can serve several profiles.
        """
        hook = skill.bundle_root / "start.sh"
        if not hook.is_file():
            return
        bash = shutil.which("bash")
        if bash is None:
            raise RuntimeManagerError(
                "bundle start.sh requires bash, but bash is not available on PATH"
            )
        self._log(skill.name, f"running bundle start.sh hook (profile={profile_name})")
        env = {
            **os.environ,
            "PAOS_SKILL_PROFILE": profile_name,
            "PAOS_HOOK_PYTHON": sys.executable,
            "PAOS_SKILL_ROOT": str(skill.bundle_root),
            "PAOS_SKILL_NAME": skill.name,
            "PAOS_SKILL_VERSION": skill.version,
        }
        try:
            result = subprocess.run(
                [bash, str(hook), skill.name, skill.version],
                check=False,
                env=env,
            )
        except OSError as exc:
            raise RuntimeManagerError("failed to execute bundle start.sh hook") from exc
        if result.returncode != 0:
            raise RuntimeManagerError(
                "bundle start.sh hook exited with code "
                f"{result.returncode}; skill start aborted"
            )
        self._log(skill.name, "bundle start.sh hook completed")

    def _ensure_dora_up(
        self,
        skill: SkillManifest,
        profile: RuntimeProfile,
        binary_root: Path,
    ) -> None:
        dora = shutil.which("dora")
        assert dora is not None
        cwd = self._launch_dataflow(binary_root, profile).parent
        check = self._run([dora, "check"], cwd=cwd, timeout=5)
        if check.returncode == 0:
            return
        self.logs_root.mkdir(parents=True, exist_ok=True)
        coordinator_log = self.logs_root / "dora-coordinator.log"
        with coordinator_log.open("ab") as output:
            try:
                env = {
                    **os.environ,
                    **profile.environment,
                    "FORGE_RUNTIME_BIN": str(binary_root),
                    "PAOS_SKILL_ROOT": str(skill.bundle_root),
                    "PAOS_SKILL_NAME": skill.name,
                    "PAOS_SKILL_VERSION": skill.version,
                }
                subprocess.Popen(
                    [dora, "up"],
                    cwd=cwd,
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                raise RuntimeManagerError("failed to launch dora up") from exc
        deadline = time.monotonic() + min(self.health_timeout_s, 10.0)
        while time.monotonic() < deadline:
            if self._run([dora, "check"], cwd=cwd, timeout=5).returncode == 0:
                return
            time.sleep(self.poll_interval_s)
        raise RuntimeManagerError("dora up did not become ready before the timeout")

    def _start_flow(
        self,
        flow_name: str,
        skill: SkillManifest,
        profile: RuntimeProfile,
        binary_root: Path,
    ) -> None:
        dora = shutil.which("dora")
        assert dora is not None
        dataflow = self._launch_dataflow(binary_root, profile)
        env = {
            **os.environ,
            **profile.environment,
            "FORGE_RUNTIME_BIN": str(binary_root),
            "PAOS_SKILL_ROOT": str(skill.bundle_root),
            "PAOS_SKILL_NAME": skill.name,
            "PAOS_SKILL_VERSION": skill.version,
        }
        self.logs_root.mkdir(parents=True, exist_ok=True)
        launch_log = self.logs_root / f"{flow_name}-dora.log"
        with launch_log.open("ab") as output:
            try:
                process = subprocess.Popen(
                    [dora, "start", "--name", flow_name, dataflow.name],
                    cwd=dataflow.parent,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                raise RuntimeManagerError("failed to launch dora start") from exc
        time.sleep(min(0.25, self.poll_interval_s))
        if process.poll() not in {None, 0}:
            raise RuntimeManagerError(
                "dora start exited before the flow was admitted; inspect Skill runtime logs"
            )

    def _stop_flow(self, flow_name: str, *, force: bool, check: bool = True) -> None:
        dora = shutil.which("dora")
        if dora is None:
            raise RuntimeManagerError("dora is not installed or not available on PATH")
        command = [dora, "stop", "--name", flow_name]
        command.extend(["--force"] if force else ["--grace-duration", "5s"])
        result = self._run(command, cwd=self.runtime_root, timeout=15)
        if check and result.returncode != 0 and self._flow_running(flow_name):
            raise RuntimeManagerError("dora stop failed and the flow is still running")

    def _flow_running(self, flow_name: str) -> bool:
        dora = shutil.which("dora")
        if dora is None:
            return False
        result = self._run(
            [dora, "list", "--format", "json", "--name", flow_name],
            timeout=5,
        )
        if result.returncode != 0:
            return False
        return self._has_active_flow(
            self._parse_flow_list(result.stdout or ""), flow_name
        )

    @staticmethod
    def _parse_flow_list(output: str) -> list[dict[str, Any]]:
        """Parse ``dora list --format json`` output.

        Some Dora builds emit a single JSON array; others emit NDJSON (one JSON
        object per line). Accept both so the live flow is not misread.
        """
        text = (output or "").strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
        entries: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                entries.append(item)
        return entries

    @staticmethod
    def _has_active_flow(flows: list[dict[str, Any]], flow_name: str) -> bool:
        """Return True only when ``flow_name`` has a live Running flow.

        Historical Dora rows with the same name linger as Finished/Failed/
        Succeeded with ``nodes == 0``; the live flow is the running entry
        (prefer one that reports participating nodes).
        """
        running: list[dict[str, Any]] = []
        for item in flows:
            if item.get("name") not in {None, flow_name}:
                continue
            if str(item.get("status", "")).lower() == "running":
                running.append(item)
        if not running:
            return False
        with_nodes = [item for item in running if item.get("nodes") not in (None, 0)]
        return bool(with_nodes) if with_nodes else True

    def _startup_timeout_s(self, profile: RuntimeProfile) -> float:
        if profile.startup_timeout_s is None:
            return self.health_timeout_s
        return profile.startup_timeout_s

    def _wait_until_ready(
        self,
        manifest: SkillManifest,
        flow_name: str,
        *,
        tools: tuple[str, ...],
        timeout_s: float,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        last_reason = "Gateway GET /tools is unavailable"
        while time.monotonic() < deadline:
            if not self._flow_running(flow_name):
                last_reason = "Dora flow is not running"
            elif self._gateway_snapshot(manifest) is None:
                last_reason = "Gateway GET /tools is unavailable"
            else:
                contexts = self._tool_context_readiness(manifest, tools)
                missing = [
                    tool for tool in tools if not contexts.get(tool, False)
                ]
                if not missing:
                    return
                last_reason = f"Tool context is not ready: {', '.join(missing)}"
            time.sleep(self.poll_interval_s)
        raise RuntimeManagerError(f"Runtime health check timed out: {last_reason}")

    def _gateway_snapshot(self, manifest: SkillManifest) -> dict[str, Any] | None:
        try:
            value = self._get_json(f"{manifest.gateway_url}/tools")
        except RuntimeManagerError:
            return None
        return value if value.get("ok") is True else None

    def _tool_context_readiness(
        self, manifest: SkillManifest, tools: tuple[str, ...]
    ) -> dict[str, bool]:
        readiness: dict[str, bool] = {}
        for tool_id in tools:
            try:
                value = self._get_json(
                    f"{manifest.gateway_url}/tools/{quote(tool_id, safe='')}/context"
                )
                data = value.get("data", {})
                readiness[tool_id] = (
                    value.get("ok") is True
                    and isinstance(data, dict)
                    and data.get("ready") is True
                    and data.get("binding_error") is None
                )
            except RuntimeManagerError:
                readiness[tool_id] = False
        return readiness

    def _get_json(self, url: str) -> dict[str, Any]:
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=2.0) as response:
                if response.status != 200:
                    raise RuntimeManagerError("Gateway health request failed")
                value = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeManagerError("Gateway health request failed") from exc
        if not isinstance(value, dict):
            raise RuntimeManagerError("Gateway health response must be a JSON object")
        return value

    @staticmethod
    def _request_gateway_control(
        url: str,
        *,
        reference: str,
        operation: str,
    ) -> dict[str, Any]:
        """Best-effort force-stop control without treating acceptance as termination."""
        request = Request(
            url,
            data=b"{}",
            method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=2.0) as response:
                outcome = "accepted" if response.status in {200, 202} else "rejected"
                return {
                    "reference": reference,
                    "operation": operation,
                    "outcome": outcome,
                    "http_status": response.status,
                    "terminal_proven": False,
                }
        except HTTPError as exc:
            return {
                "reference": reference,
                "operation": operation,
                "outcome": "rejected",
                "http_status": exc.code,
                "terminal_proven": False,
            }
        except (URLError, TimeoutError, OSError) as exc:
            return {
                "reference": reference,
                "operation": operation,
                "outcome": "unknown",
                "error_type": type(exc).__name__,
                "terminal_proven": False,
            }

    @staticmethod
    def _run(
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeManagerError(f"{Path(command[0]).name} command failed") from exc

    def _log(self, skill_name: str, message: str) -> None:
        self.logs_root.mkdir(parents=True, exist_ok=True)
        with (self.logs_root / f"{skill_name}.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_now()} {message}\n")

    @staticmethod
    def _safe_error(error: Exception) -> str:
        if isinstance(error, RuntimeManagerError):
            return str(error)
        return f"{type(error).__name__} during runtime lifecycle operation"
