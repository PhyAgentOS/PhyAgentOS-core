"""Agent-owned task aggregation over the single Forge Tool API execution plane."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from PhyAgentOS.config.schema import ForgeConfig
from PhyAgentOS.forge.binding import (
    BoundToolSpec,
    ForgeSkillBinding,
    ForgeSkillBindingError,
    ForgeSkillBindingResolver,
    canonical_sha256,
)
from PhyAgentOS.forge.evidence import ForgeEvidenceWriter
from PhyAgentOS.forge.observation import ForgeObservationCollector
from PhyAgentOS.forge.tool_client import ForgeToolAPIError, ForgeToolClient
from PhyAgentOS.verification.contracts import (
    TaskVerificationContract,
    VerificationAttempt,
    VerificationVerdict,
    utc_now,
)


class AgentTaskError(RuntimeError):
    """Raised when an AgentTask operation violates its lifecycle contract."""


class AgentTaskBusyError(AgentTaskError):
    """Raised when the global non-terminal AgentTask slot is occupied."""


class AgentTaskStatus(StrEnum):
    EXECUTING = "executing"
    CANCELLING = "cancelling"
    AWAITING_REPLAN = "awaiting_replan"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES = {
    AgentTaskStatus.SUCCEEDED,
    AgentTaskStatus.FAILED,
    AgentTaskStatus.CANCELLED,
}

TERMINAL_TOOL_STATUSES = {
    "succeeded",
    "failed",
    "cancelled",
    "stopped",
    "unknown",
}


class ToolExecutionRecord(BaseModel):
    """One Query execution or one Gateway-owned Action invocation reference."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["tool_execution_record_v2"] = "tool_execution_record_v2"
    record_id: str
    revision_id: str
    tool_id: str
    semantics: Literal["query", "action", "session"]
    skill_binding_id: str | None = None
    tool_spec_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    caller_id: str
    ownership: Literal["task", "runtime", "shared"] = "task"
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal[
        "pending",
        "accepted",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "stopped",
        "unknown",
    ] = "pending"
    invocation_id: str | None = None
    attempt_id: str | None = None
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("record_id", "revision_id", "tool_id")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
            raise ValueError("Tool execution identifiers must be non-empty and path-safe")
        return normalized

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TOOL_STATUSES


class PlanRevision(BaseModel):
    """Append-only plan generation within one stable AgentTask identity."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["plan_revision_v2"] = "plan_revision_v2"
    revision_id: str
    number: int = Field(ge=1)
    reason: str = Field(min_length=1)
    skill_binding_id: str | None = None
    execution_records: list[ToolExecutionRecord] = Field(default_factory=list)
    verdict: VerificationVerdict | None = None
    verification_attempts: list[VerificationAttempt] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    closed_at: datetime | None = None


class AgentTaskRecord(BaseModel):
    """PAOS task aggregate; never a second robot execution protocol."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["agent_task_record_v2"] = "agent_task_record_v2"
    task_id: str
    task_description: str = Field(min_length=1)
    verification: TaskVerificationContract = Field(default_factory=TaskVerificationContract)
    status: AgentTaskStatus = AgentTaskStatus.EXECUTING
    revisions: list[PlanRevision]
    active_revision_id: str
    primary_skill_binding: ForgeSkillBinding | None = None
    supporting_skill_bindings: list[ForgeSkillBinding] = Field(default_factory=list)
    runtime_snapshot_ref: str | None = None
    verdict: VerificationVerdict | None = None
    verification_attempts: list[VerificationAttempt] = Field(default_factory=list)
    before_snapshot_ref: str | None = None
    after_snapshot_ref: str | None = None
    evidence_bundle_ref: str | None = None
    evidence_errors: list[str] = Field(default_factory=list)
    cancellation_requested: bool = False
    replan_deadline: datetime | None = None
    origin_session_key: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    terminal_at: datetime | None = None

    @field_validator("task_id", "active_revision_id")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
            raise ValueError("AgentTask identifiers must be non-empty and path-safe")
        return normalized

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    @property
    def active_revision(self) -> PlanRevision:
        for revision in self.revisions:
            if revision.revision_id == self.active_revision_id:
                return revision
        raise AgentTaskError("active PlanRevision is missing")

    @property
    def execution_records(self) -> list[ToolExecutionRecord]:
        return [item for revision in self.revisions for item in revision.execution_records]


class AgentTaskStore:
    """Transactional SQLite store enforcing one global non-terminal AgentTask."""

    def __init__(self, workspace: str | Path) -> None:
        root = Path(workspace).expanduser().resolve() / ".paos" / "agent_tasks"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "tasks.sqlite3"
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS agent_tasks_status_idx
                    ON agent_tasks(status);
                CREATE TABLE IF NOT EXISTS agent_task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES agent_tasks(task_id)
                );
                """
            )

    def create(self, record: AgentTaskRecord) -> AgentTaskRecord:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT task_id FROM agent_tasks WHERE status NOT IN (?, ?, ?) LIMIT 1",
                tuple(item.value for item in TERMINAL_TASK_STATUSES),
            ).fetchone()
            if active is not None:
                raise AgentTaskBusyError(
                    f"AgentTask {active['task_id']} is still non-terminal"
                )
            self._insert(connection, record)
            self._event(connection, record.task_id, "task_created", {})
            connection.commit()
        return record

    def get(self, task_id: str) -> AgentTaskRecord:
        with self._lock, self._connection() as connection:
            return self._get(connection, task_id)

    def update(
        self,
        task_id: str,
        mutate: Callable[[AgentTaskRecord], None],
        *,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> AgentTaskRecord:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = self._get(connection, task_id)
            mutate(record)
            record.updated_at = utc_now()
            if record.terminal and record.terminal_at is None:
                record.terminal_at = record.updated_at
            connection.execute(
                "UPDATE agent_tasks SET status = ?, record_json = ?, updated_at = ? "
                "WHERE task_id = ?",
                (
                    record.status.value,
                    record.model_dump_json(),
                    record.updated_at.isoformat(),
                    task_id,
                ),
            )
            self._event(connection, task_id, event_type, payload or {})
            connection.commit()
            return record

    def active(self) -> AgentTaskRecord | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM agent_tasks WHERE status NOT IN (?, ?, ?) "
                "ORDER BY created_at LIMIT 1",
                tuple(item.value for item in TERMINAL_TASK_STATUSES),
            ).fetchone()
        return None if row is None else AgentTaskRecord.model_validate_json(row["record_json"])

    def find_invocation(self, invocation_id: str) -> tuple[AgentTaskRecord, ToolExecutionRecord] | None:
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT record_json FROM agent_tasks").fetchall()
        for row in rows:
            task = AgentTaskRecord.model_validate_json(row["record_json"])
            for record in task.execution_records:
                if record.invocation_id == invocation_id:
                    return task, record
        return None

    def has_event(self, task_id: str, event_type: str) -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM agent_task_events WHERE task_id=? AND event_type=? LIMIT 1",
                (task_id, event_type),
            ).fetchone() is not None

    def events(self, task_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT event_type, created_at, payload_json FROM agent_task_events "
                "WHERE task_id = ? ORDER BY event_id DESC LIMIT ?",
                (task_id, max(1, int(limit))),
            ).fetchall()
        return [
            {
                "event_type": row["event_type"],
                "created_at": row["created_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in reversed(rows)
        ]

    @staticmethod
    def _insert(connection: sqlite3.Connection, record: AgentTaskRecord) -> None:
        connection.execute(
            "INSERT INTO agent_tasks(task_id, status, record_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                record.task_id,
                record.status.value,
                record.model_dump_json(),
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )

    @staticmethod
    def _get(connection: sqlite3.Connection, task_id: str) -> AgentTaskRecord:
        row = connection.execute(
            "SELECT record_json FROM agent_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise AgentTaskError(f"AgentTask not found: {task_id}")
        return AgentTaskRecord.model_validate_json(row["record_json"])

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO agent_task_events(task_id, event_type, created_at, payload_json) "
            "VALUES (?, ?, ?, ?)",
            (task_id, event_type, utc_now().isoformat(), json.dumps(payload, ensure_ascii=False)),
        )


class AgentTaskCoordinator:
    """Aggregate Tool API facts, evidence and semantic verification by user task."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        config: ForgeConfig,
        client: ForgeToolClient,
        verifier: Any | None = None,
        experience: Any | None = None,
        binding_resolver: ForgeSkillBindingResolver | None = None,
        activation_manager: Any | None = None,
        runtime_invocation_ids: Any | None = None,
        runtime_session_ids: Any | None = None,
        runtime_task_binding_ids: Any | None = None,
        store: AgentTaskStore | None = None,
        max_replans: int = 2,
        replan_timeout_s: float = 120.0,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.config = config
        self.client = client
        self.verifier = verifier
        self.experience = experience
        self.binding_resolver = binding_resolver
        self.activation_manager = activation_manager
        self.runtime_invocation_ids = runtime_invocation_ids
        self.runtime_session_ids = runtime_session_ids
        self.runtime_task_binding_ids = runtime_task_binding_ids
        self.store = store or AgentTaskStore(self.workspace)
        from PhyAgentOS.forge.interaction.service import InteractionService
        self.interaction = InteractionService(self)
        self.max_replans = max(0, int(max_replans))
        self.replan_timeout_s = max(0.1, float(replan_timeout_s))

    def set_experience(self, experience: Any | None) -> None:
        self.experience = experience

    def set_activation_manager(self, activation_manager: Any) -> None:
        self.activation_manager = activation_manager

    def create_task(
        self,
        *,
        task_description: str,
        verification: TaskVerificationContract,
        activation_id: str | None = None,
        origin_session_key: str | None = None,
    ) -> AgentTaskRecord | Any:
        """Create synchronously only for an explicitly unbound coordinator.

        Managed Forge runtimes return an awaitable because binding freeze revalidates live
        Gateway ToolSpecs. Agent tools handle both forms; physical execution always uses the
        bound form.
        """
        if self.binding_resolver is not None:
            return self._create_task_bound(
                task_description=task_description,
                verification=verification,
                activation_id=activation_id,
                origin_session_key=origin_session_key,
            )
        if verification.mode != "off" and self.verifier is None:
            raise AgentTaskError(
                "non-off AgentTask verification requires the verification service"
            )
        task_id = f"task_{uuid4().hex[:16]}"
        revision_id = f"revision_{uuid4().hex[:16]}"
        task = AgentTaskRecord(
            task_id=task_id,
            task_description=task_description.strip(),
            verification=verification,
            revisions=[PlanRevision(revision_id=revision_id, number=1, reason="initial plan")],
            active_revision_id=revision_id,
            origin_session_key=origin_session_key,
        )
        self.store.create(task)
        if self.experience is not None and origin_session_key:
            self.experience.bind_forge_task(
                task_id,
                session_key=origin_session_key,
            )
        return task

    async def _create_task_bound(
        self,
        *,
        task_description: str,
        verification: TaskVerificationContract,
        activation_id: str | None = None,
        origin_session_key: str | None = None,
    ) -> AgentTaskRecord:
        if verification.mode != "off" and self.verifier is None:
            raise AgentTaskError(
                "non-off AgentTask verification requires the verification service"
            )
        task_id = f"task_{uuid4().hex[:16]}"
        revision_id = f"revision_{uuid4().hex[:16]}"
        binding: ForgeSkillBinding | None = None
        if self.binding_resolver is not None:
            if self.activation_manager is None or not origin_session_key or not activation_id:
                raise AgentTaskError(
                    "Forge AgentTask creation requires a primary Skill activation from this turn"
                )
            activation = self.activation_manager.require_activation(
                session_key=origin_session_key,
                activation_id=activation_id,
                role="primary",
            )
            candidate_id = activation.binding_candidate_id
            if not candidate_id:
                raise AgentTaskError("primary Skill activation has no Forge binding candidate")
            try:
                binding = await self.binding_resolver.freeze(candidate_id, task_id=task_id)
            except ForgeSkillBindingError as exc:
                raise AgentTaskError(str(exc)) from exc
            if activation.content_sha256 != binding.skill_document_sha256:
                raise AgentTaskError(
                    "activated SKILL.md does not match the installed Runtime binding"
                )
        task = AgentTaskRecord(
            task_id=task_id,
            task_description=task_description.strip(),
            verification=verification,
            revisions=[
                PlanRevision(
                    revision_id=revision_id,
                    number=1,
                    reason="initial plan",
                    skill_binding_id=binding.binding_id if binding is not None else None,
                )
            ],
            active_revision_id=revision_id,
            primary_skill_binding=binding,
            runtime_snapshot_ref=(
                f"runtime:{binding.runtime_instance_id}" if binding is not None else None
            ),
            origin_session_key=origin_session_key,
        )
        await self.interaction.freeze(task)
        if binding is not None and self.runtime_task_binding_ids is not None:
            self.runtime_task_binding_ids.add(binding.binding_id)
        try:
            self.store.create(task)
            if self.interaction.path.exists() and self.interaction.store.binding(task_id):
                self.store.update(task_id, lambda current: None, event_type="interaction_bound")
        except Exception:
            if binding is not None and self.runtime_task_binding_ids is not None:
                self.runtime_task_binding_ids.discard(binding.binding_id)
            raise
        if self.experience is not None and origin_session_key:
            self.experience.bind_forge_task(
                task_id,
                session_key=origin_session_key,
                forge_binding=binding,
            )
        return task

    def get_task(self, task_id: str) -> AgentTaskRecord:
        return self.store.get(task_id)

    def begin_revision(self, task_id: str, *, reason: str) -> AgentTaskRecord:
        self.interaction.assert_settled(task_id)
        task = self.store.get(task_id)
        if task.status != AgentTaskStatus.AWAITING_REPLAN:
            raise AgentTaskError("a new PlanRevision requires awaiting_replan status")
        if task.replan_deadline is not None and utc_now() >= task.replan_deadline:
            failed = self.store.update(
                task_id,
                lambda current: _fail_replan(
                    current, "AgentTask replan deadline expired"
                ),
                event_type="plan_revision_expired",
            )
            self._schedule_experience(failed)
            raise AgentTaskError("AgentTask replan deadline expired")
        if len(task.revisions) - 1 >= self.max_replans:
            raise AgentTaskError(f"replan budget exhausted ({self.max_replans})")

        def mutate(current: AgentTaskRecord) -> None:
            current.active_revision.closed_at = utc_now()
            revision = PlanRevision(
                revision_id=f"revision_{uuid4().hex[:16]}",
                number=len(current.revisions) + 1,
                reason=reason.strip(),
                skill_binding_id=(
                    current.primary_skill_binding.binding_id
                    if current.primary_skill_binding is not None
                    else None
                ),
            )
            current.revisions.append(revision)
            current.active_revision_id = revision.revision_id
            current.status = AgentTaskStatus.EXECUTING
            current.verdict = None
            current.replan_deadline = None

        return self.store.update(task_id, mutate, event_type="plan_revision_started")

    async def invoke_query(
        self,
        task_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        tool = await self._require_binding_tool(task_id, tool_id, "query")
        record_id, caller = self._append_execution(
            task_id, tool_id, "query", arguments, tool=tool
        )
        try:
            response = await self._execution_client(task_id).invoke_query_tool(
                tool_id, arguments, caller_id=caller, timeout_ms=timeout_ms
            )
        except Exception as exc:
            self._finish_execution(
                task_id,
                record_id,
                status="unknown",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise
        self._finish_execution(task_id, record_id, status="succeeded", response=response)
        return response

    async def start_action(
        self,
        task_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        task = self._require_executable(task_id)
        if any(
            item.semantics == "action"
            and item.tool_id == tool_id
            and item.arguments == arguments
            and item.status == "unknown"
            for item in task.execution_records
        ):
            raise AgentTaskError(
                "an identical Action has unknown remote state; reconcile it instead of resending"
            )
        tool = await self._require_binding_tool(task_id, tool_id, "action")
        if task.before_snapshot_ref is None:
            await self._capture_before(task_id)
        record_id, caller = self._append_execution(
            task_id, tool_id, "action", arguments, tool=tool
        )
        invocation_id: str | None = None
        attempt_id: str | None = None
        try:
            response = await self._execution_client(task_id).invoke_action(
                tool_id, arguments, caller_id=caller, timeout_ms=timeout_ms
            )
            data = _response_data(response)
            invocation_id = data.get("invocation_id")
            attempt_id = data.get("attempt_id")
            if not isinstance(invocation_id, str) or not invocation_id:
                raise AgentTaskError("Gateway Action response omitted invocation_id")
            if not isinstance(attempt_id, str) or not attempt_id:
                raise ForgeToolAPIError(
                    "Gateway Action response omitted attempt_id for invocation "
                    f"{invocation_id}",
                    payload=response,
                )
        except Exception as exc:
            remote_invocation_id, remote_attempt_id = _remote_identity(exc)
            invocation_id = remote_invocation_id or invocation_id
            attempt_id = remote_attempt_id or attempt_id
            self._finish_execution(
                task_id,
                record_id,
                status="unknown",
                invocation_id=invocation_id,
                attempt_id=attempt_id,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            if invocation_id and self.runtime_invocation_ids is not None:
                self._track_remote_identity(
                    task_id,
                    record_id,
                    invocation_id,
                    tracker=self.runtime_invocation_ids,
                    response={
                        "ok": False,
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                    kind="Action",
                )
            raise

        def mutate(current: AgentTaskRecord) -> None:
            record = _task_execution(current, record_id)
            record.status = "accepted"
            record.invocation_id = invocation_id
            record.attempt_id = attempt_id
            record.response = response
            record.evidence_refs = [f"invocation:{invocation_id}"]
            record.updated_at = utc_now()

        self.store.update(task_id, mutate, event_type="action_accepted")
        return self._track_remote_identity(
            task_id,
            record_id,
            invocation_id,
            tracker=self.runtime_invocation_ids,
            response=response,
            kind="Action",
        )

    def observe_action(
        self, task_id: str, invocation_id: str, response: dict[str, Any]
    ) -> None:
        task = self.store.get(task_id)
        record = _owned_execution(task, invocation_id, semantics="action")
        if task.terminal:
            return
        status = _tool_status(response, default=record.status)

        def mutate(current: AgentTaskRecord) -> None:
            target = _task_execution(current, record.record_id)
            target.status = status
            target.response = response
            target.updated_at = utc_now()

        self.store.update(task.task_id, mutate, event_type="action_observed")
        if (
            status in TERMINAL_TOOL_STATUSES - {"unknown"}
            and self.runtime_invocation_ids is not None
        ):
            from PhyAgentOS.forge.interaction.client import trusted_runtime
            runtime = trusted_runtime(self, task_id)
            (runtime.invocation_ids if runtime is not None else self.runtime_invocation_ids).discard(invocation_id)

    def require_action_invocation(
        self, task_id: str, invocation_id: str
    ) -> ToolExecutionRecord:
        return _owned_execution(
            self.store.get(task_id), invocation_id, semantics="action"
        )

    def require_session_invocation(
        self, task_id: str, invocation_id: str
    ) -> ToolExecutionRecord:
        return _owned_execution(
            self.store.get(task_id), invocation_id, semantics="session"
        )

    def record_cancel_response(
        self, task_id: str, invocation_id: str, response: dict[str, Any]
    ) -> None:
        task = self.store.get(task_id)
        record = _owned_execution(task, invocation_id, semantics="action")
        if task.terminal:
            return

        def mutate(current: AgentTaskRecord) -> None:
            target = _task_execution(current, record.record_id)
            target.response = response
            target.updated_at = utc_now()

        self.store.update(task.task_id, mutate, event_type="action_cancel_requested")

    async def start_session(
        self,
        task_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        *,
        ownership: Literal["task", "shared"] = "task",
    ) -> dict[str, Any]:
        from PhyAgentOS.forge.interaction.client import trusted_runtime
        if self.interaction.managed(task_id) and trusted_runtime(self, task_id) is None:
            return await self.interaction.start(task_id, tool_id, arguments, ownership)
        if ownership not in {"task", "shared"}:
            raise AgentTaskError("runtime-owned Sessions may only be created by RuntimeManager")
        tool = await self._require_binding_tool(task_id, tool_id, "session")
        record_id, caller = self._append_execution(
            task_id,
            tool_id,
            "session",
            arguments,
            tool=tool,
            ownership=ownership,
        )
        invocation_id: str | None = None
        attempt_id: str | None = None
        try:
            response = await self._execution_client(task_id).start_session(
                tool_id, arguments, caller_id=caller
            )
            data = _response_data(response)
            invocation_id = data.get("invocation_id")
            attempt_id = data.get("attempt_id")
            if not isinstance(invocation_id, str) or not invocation_id:
                raise AgentTaskError("Gateway Session response omitted invocation_id")
            if not isinstance(attempt_id, str):
                attempt_id = None
        except Exception as exc:
            remote_invocation_id, remote_attempt_id = _remote_identity(exc)
            invocation_id = remote_invocation_id or invocation_id
            attempt_id = remote_attempt_id or attempt_id
            self._finish_execution(
                task_id,
                record_id,
                status="unknown",
                invocation_id=invocation_id,
                attempt_id=attempt_id,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            if invocation_id and self.runtime_session_ids is not None:
                self._track_remote_identity(
                    task_id,
                    record_id,
                    invocation_id,
                    tracker=self.runtime_session_ids,
                    response={
                        "ok": False,
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                    kind="Session",
                )
            raise

        def mutate(current: AgentTaskRecord) -> None:
            record = _task_execution(current, record_id)
            record.status = "accepted"
            record.invocation_id = invocation_id
            record.attempt_id = attempt_id
            record.response = response
            record.evidence_refs = [f"session:{invocation_id}"]
            record.updated_at = utc_now()

        self.store.update(task_id, mutate, event_type="session_accepted")
        return self._track_remote_identity(
            task_id,
            record_id,
            invocation_id,
            tracker=self.runtime_session_ids,
            response=response,
            kind="Session",
        )

    def observe_session(
        self, task_id: str, invocation_id: str, response: dict[str, Any]
    ) -> None:
        task = self.store.get(task_id)
        record = _owned_execution(task, invocation_id, semantics="session")
        status = _tool_status(response, default=record.status)

        def mutate(current: AgentTaskRecord) -> None:
            target = _task_execution(current, record.record_id)
            target.status = status
            target.response = response
            target.updated_at = utc_now()

        self.store.update(task_id, mutate, event_type="session_observed")
        if (
            status in TERMINAL_TOOL_STATUSES - {"unknown"}
            and self.runtime_session_ids is not None
        ):
            from PhyAgentOS.forge.interaction.client import trusted_runtime
            runtime = trusted_runtime(self, task_id)
            (runtime.session_ids if runtime is not None else self.runtime_session_ids).discard(invocation_id)

    async def stop_session(self, task_id: str, invocation_id: str) -> dict[str, Any]:
        from PhyAgentOS.forge.interaction.client import trusted_runtime
        if self.interaction.managed(task_id) and trusted_runtime(self, task_id) is None:
            self.require_session_invocation(task_id, invocation_id)
            await self.interaction.cancel(task_id, "session_stop_requested")
            return {"ok": True, "data": {"status": "requested"}}
        task = self.store.get(task_id)
        record = _owned_execution(task, invocation_id, semantics="session")
        if record.ownership != "task":
            raise AgentTaskError(
                f"{record.ownership}-owned Session must be stopped by its Runtime owner"
            )
        self.store.update(task_id, lambda current: None, event_type="session_stop_intent",
                          payload={"invocation_id": invocation_id})
        response = await self._execution_client(task_id).stop_session(invocation_id)

        def mutate(current: AgentTaskRecord) -> None:
            target = _task_execution(current, record.record_id)
            target.response = response
            target.updated_at = utc_now()

        self.store.update(task_id, mutate, event_type="session_stop_requested")
        return response

    async def cancel_task(self, task_id: str, *, reason: str) -> AgentTaskRecord:
        if self.interaction.managed(task_id):
            return await self.interaction.cancel(task_id, reason)
        task = self.store.get(task_id)
        if task.terminal:
            return task
        pending = [
            item
            for item in task.execution_records
            if (
                item.semantics == "action"
                or (item.semantics == "session" and item.ownership == "task")
            )
            and not item.terminal
            and item.invocation_id
        ]
        responses: dict[str, Any] = {}
        for item in pending:
            invocation_id = item.invocation_id
            assert invocation_id is not None
            try:
                responses[invocation_id] = (
                    await self._execution_client(task_id).stop_session(invocation_id)
                    if item.semantics == "session"
                    else await self._execution_client(task_id).cancel_invocation(invocation_id)
                )
            except Exception as exc:
                responses[invocation_id] = {
                    "ok": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }

        def mutate(current: AgentTaskRecord) -> None:
            current.cancellation_requested = True
            has_owned_execution = any(
                item.semantics == "action"
                or (item.semantics == "session" and item.ownership == "task")
                for item in current.execution_records
            )
            current.status = (
                AgentTaskStatus.CANCELLING
                if pending or has_owned_execution
                else _cancel_terminal_status(current)
            )
            current.evidence_errors.append(f"task cancellation requested: {reason.strip()}")
            for invocation_id, response in responses.items():
                target = next(
                    item for item in current.execution_records if item.invocation_id == invocation_id
                )
                target.response = response
                target.updated_at = utc_now()

        result = self.store.update(
            task_id, mutate, event_type="task_cancel_requested"
        )
        if result.terminal:
            self._schedule_experience(result)
        return result

    async def finalize_task(self, task_id: str) -> AgentTaskRecord:
        await self.interaction.validate_finalization(task_id)
        task = self.store.get(task_id)
        if task.terminal:
            return task
        pending = [
            item.invocation_id or item.record_id
            for item in task.execution_records
            if (
                item.semantics == "action"
                or (item.semantics == "session" and item.ownership == "task")
            )
            and not item.terminal
        ]
        if pending:
            raise AgentTaskError(
                "cannot finalize while task-owned Action/Session invocation(s) are non-terminal: "
                + ", ".join(pending)
            )
        if not task.execution_records:
            raise AgentTaskError("cannot finalize an AgentTask without Tool executions")
        await self._capture_after(task_id)
        task = self.store.get(task_id)
        if task.cancellation_requested:
            status = AgentTaskStatus.CANCELLED if self.interaction.managed(task_id) else _cancel_terminal_status(task)
            result = self.store.update(
                task_id,
                lambda current: setattr(current, "status", status),
                event_type="task_cancelled",
            )
            self._schedule_experience(result)
            return result

        mode = task.verification.mode
        if mode == "off":
            status = (
                AgentTaskStatus.SUCCEEDED
                if _execution_facts_succeeded(task)
                else AgentTaskStatus.FAILED
            )
            result = self.store.update(
                task_id,
                lambda current: setattr(current, "status", status),
                event_type="task_finalized_from_execution",
            )
            self._schedule_experience(result)
            return result
        if self.verifier is None:
            return self._verification_error(
                task_id, "semantic verification is enabled but verifier is unavailable"
            )

        lessons = (
            self.experience.verification_lessons_for_root(task_id)
            if self.experience is not None
            else "[]"
        )
        try:
            verdict, _request, attempt = await self.verifier.verify_agent_task(
                task,
                events=self.store.events(task_id),
                lessons=lessons,
                source="auto",
                mode="apply",
            )
        except Exception as exc:
            return self._verification_error(
                task_id, str(exc) or type(exc).__name__
            )

        await self.interaction.validate_finalization(task_id)
        if self.interaction.managed(task_id) and self.store.get(task_id).cancellation_requested:
            return await self.finalize_task(task_id)

        def mutate(current: AgentTaskRecord) -> None:
            current.verdict = verdict
            current.verification_attempts.append(attempt)
            current.active_revision.verdict = verdict
            current.active_revision.verification_attempts.append(attempt)
            if current.verification.mode == "audit":
                current.status = (
                    AgentTaskStatus.SUCCEEDED
                    if _execution_facts_succeeded(current)
                    else AgentTaskStatus.FAILED
                )
            elif current.verification.mode == "recovery" and verdict.verdict == "replan_required":
                if len(current.revisions) - 1 >= self.max_replans:
                    current.status = AgentTaskStatus.FAILED
                    current.evidence_errors.append(
                        f"replan limit reached ({self.max_replans}): {verdict.reason}"
                    )
                else:
                    current.status = AgentTaskStatus.AWAITING_REPLAN
                    current.replan_deadline = utc_now() + timedelta(
                        seconds=self.replan_timeout_s
                    )
            elif verdict.verdict == "success":
                current.status = AgentTaskStatus.SUCCEEDED
            else:
                current.status = AgentTaskStatus.FAILED

        result = self.store.update(task_id, mutate, event_type="task_verified")
        if result.terminal:
            self._schedule_experience(result)
        return result

    def _verification_error(self, task_id: str, message: str) -> AgentTaskRecord:
        self.interaction.assert_settled(task_id)
        task = self.store.get(task_id)
        status = (
            AgentTaskStatus.SUCCEEDED
            if task.verification.mode == "audit"
            and _execution_facts_succeeded(task)
            else AgentTaskStatus.FAILED
        )

        def mutate(current: AgentTaskRecord) -> None:
            current.status = status
            current.evidence_errors.append(f"verification failed: {message}")
            current.verification_attempts.append(
                VerificationAttempt(
                    attempt_id=f"verification_{uuid4().hex[:12]}",
                    error=message,
                )
            )
            current.active_revision.verification_attempts.append(
                current.verification_attempts[-1]
            )

        result = self.store.update(
            task_id,
            mutate,
            event_type="task_verification_failed",
        )
        self._schedule_experience(result)
        return result

    async def reconcile_nonterminal(self) -> AgentTaskRecord | None:
        """Repair local execution facts from Gateway GETs without redispatching POSTs."""
        if self.interaction.supervisor is not None:
            await self.interaction.supervisor.recover()
        task = self.store.active()
        if task is not None and self.interaction.managed(task.task_id):
            return task
        if task is None:
            return None
        binding = task.primary_skill_binding
        if binding is not None and self.runtime_task_binding_ids is not None:
            self.runtime_task_binding_ids.add(binding.binding_id)
        for record in task.execution_records:
            if record.terminal or record.semantics == "query":
                continue
            if not record.invocation_id:
                self._finish_execution(
                    task.task_id,
                    record.record_id,
                    status="unknown",
                    error={
                        "type": "RecoveryUnknown",
                        "message": (
                            "dispatch intent has no invocation identity; request was not resent"
                        ),
                    },
                )
                continue
            tracker = (
                self.runtime_session_ids
                if record.semantics == "session"
                else self.runtime_invocation_ids
            )
            if tracker is not None:
                tracker.add(record.invocation_id)
            try:
                response = await self._execution_client(task.task_id).invocation_status(record.invocation_id)
            except Exception as exc:
                self._finish_execution(
                    task.task_id,
                    record.record_id,
                    status="unknown",
                    invocation_id=record.invocation_id,
                    attempt_id=record.attempt_id,
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
                continue
            if record.semantics == "session":
                self.observe_session(task.task_id, record.invocation_id, response)
            else:
                self.observe_action(task.task_id, record.invocation_id, response)
        return self.store.get(task.task_id)

    def _execution_client(self, task_id):
        from PhyAgentOS.forge.interaction.client import trusted_runtime
        runtime = trusted_runtime(self, task_id)
        return runtime.client if runtime is not None else self.client

    async def cancel_action(self, task_id: str, invocation_id: str):
        self.require_action_invocation(task_id, invocation_id)
        if self.interaction.managed(task_id):
            await self.interaction.cancel(task_id, "action_cancel_requested")
            return {"ok": True, "data": {"status": "requested"}}
        response = await self.client.cancel_invocation(invocation_id)
        self.record_cancel_response(task_id, invocation_id, response)
        return response

    def capabilities_summary(self) -> str:
        return (
            "Forge execution uses only an activated Skill, a frozen AgentTask binding, and the "
            "Gateway Tool API. Query is read-only; Action admission is not task success; "
            "task-owned Sessions must be stopped before finalization."
        )

    def _require_executable(self, task_id: str) -> AgentTaskRecord:
        task = self.store.get(task_id)
        if task.status != AgentTaskStatus.EXECUTING:
            raise AgentTaskError(
                f"AgentTask {task_id} is not accepting Tool calls: {task.status.value}"
            )
        return task

    async def _require_binding_tool(
        self,
        task_id: str,
        tool_id: str,
        semantics: Literal["query", "action", "session"],
    ) -> BoundToolSpec:
        self.interaction.guard_call(task_id, tool_id, semantics)
        task = self._require_executable(task_id)
        binding = task.primary_skill_binding
        if binding is None and self.binding_resolver is None:
            return BoundToolSpec(
                tool_id=tool_id,
                semantics=semantics,
                spec_sha256=canonical_sha256(
                    {"tool_id": tool_id, "semantics": semantics, "unbound": True}
                ),
                ready_at_binding=True,
            )
        if binding is None or self.binding_resolver is None:
            raise AgentTaskError("AgentTask has no frozen primary Forge Skill binding")
        try:
            from PhyAgentOS.forge.interaction.client import trusted_runtime
            runtime = trusted_runtime(self, task_id)
            if runtime is not None:
                return await self.binding_resolver.validate_tool(binding, tool_id, semantics, runtime=runtime)
            return await self.binding_resolver.validate_tool(binding, tool_id, semantics)
        except ForgeSkillBindingError as exc:
            raise AgentTaskError(str(exc)) from exc

    def _append_execution(
        self,
        task_id: str,
        tool_id: str,
        semantics: Literal["query", "action", "session"],
        arguments: dict[str, Any],
        *,
        tool: BoundToolSpec,
        ownership: Literal["task", "runtime", "shared"] = "task",
    ) -> tuple[str, str]:
        task = self._require_executable(task_id)
        record_id = f"tool_{uuid4().hex[:16]}"
        caller_id = f"paos:{task_id}:{task.active_revision_id}:{record_id}"

        def mutate(current: AgentTaskRecord) -> None:
            if current.cancellation_requested or current.status != AgentTaskStatus.EXECUTING:
                raise AgentTaskError("task stopped accepting dispatch")
            current.active_revision.execution_records.append(
                ToolExecutionRecord(
                    record_id=record_id,
                    revision_id=current.active_revision_id,
                    tool_id=tool_id,
                    semantics=semantics,
                    skill_binding_id=(
                        current.primary_skill_binding.binding_id
                        if current.primary_skill_binding is not None
                        else None
                    ),
                    tool_spec_sha256=tool.spec_sha256,
                    caller_id=caller_id,
                    ownership=ownership,
                    arguments=dict(arguments),
                    evidence_refs=[f"tool:{record_id}"],
                )
            )

        self.store.update(task_id, mutate, event_type=f"{semantics}_started")
        return record_id, caller_id

    def _finish_execution(
        self,
        task_id: str,
        record_id: str,
        *,
        status: str,
        invocation_id: str | None = None,
        attempt_id: str | None = None,
        response: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        def mutate(current: AgentTaskRecord) -> None:
            record = _task_execution(current, record_id)
            record.status = status  # type: ignore[assignment]
            record.invocation_id = invocation_id or record.invocation_id
            record.attempt_id = attempt_id or record.attempt_id
            record.response = response
            record.error = error
            record.updated_at = utc_now()

        self.store.update(task_id, mutate, event_type="tool_execution_finished")

    def _track_remote_identity(
        self,
        task_id: str,
        record_id: str,
        invocation_id: str,
        *,
        tracker: Any | None,
        response: dict[str, Any],
        kind: str,
    ) -> dict[str, Any]:
        """Retain an admitted remote identity even when Runtime-state tracking fails."""
        from PhyAgentOS.forge.interaction.client import trusted_runtime
        runtime = trusted_runtime(self, task_id)
        if runtime is not None:
            tracker = runtime.session_ids if kind == "Session" else runtime.invocation_ids
        if tracker is None:
            return response
        try:
            tracker.add(invocation_id)
            return response
        except Exception as exc:
            enriched = dict(response)
            warnings = list(enriched.get("paos_warnings", []))
            warnings.append(
                {
                    "type": "local_tracking",
                    "message": (
                        f"{kind} {invocation_id} was accepted but Runtime tracking failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                }
            )
            enriched["paos_warnings"] = warnings

            def mutate(current: AgentTaskRecord) -> None:
                record = _task_execution(current, record_id)
                record.response = enriched
                record.updated_at = utc_now()
                current.evidence_errors.append(warnings[-1]["message"])

            self.store.update(
                task_id,
                mutate,
                event_type="runtime_tracking_failed",
                payload={"invocation_id": invocation_id, "kind": kind.lower()},
            )
            return enriched

    async def _capture_before(self, task_id: str) -> None:
        if self.interaction.managed(task_id):
            return  # Initial executor snapshot is recorded before the first proposal.
        task = self.store.get(task_id)
        writer = ForgeEvidenceWriter(
            self.workspace,
            task_id,
            "agent_task",
            artifact_namespace="agent_tasks",
        )
        collector: ForgeObservationCollector | None = None
        errors: list[str] = []
        reference: str | None = None
        try:
            collector = self._collector(task)
            await collector.start()
            snapshot = await collector.wait_for_before(self.config.evidence.capture_timeout_s)
            reference = writer.write_snapshot("before", snapshot)
        except Exception as exc:
            errors.append(str(exc) or type(exc).__name__)
        finally:
            if collector is not None:
                errors.extend(collector.errors)
                await collector.close()

        def mutate(current: AgentTaskRecord) -> None:
            current.before_snapshot_ref = reference
            current.evidence_errors.extend(errors)

        self.store.update(task_id, mutate, event_type="before_evidence_captured")

    async def _capture_after(self, task_id: str) -> None:
        if self.interaction.managed(task_id):
            from PhyAgentOS.forge.interaction.evidence import write_task_evidence
            write_task_evidence(self, task_id)
            return
        task = self.store.get(task_id)
        writer = ForgeEvidenceWriter(
            self.workspace,
            task_id,
            "agent_task",
            artifact_namespace="agent_tasks",
        )
        collector: ForgeObservationCollector | None = None
        errors: list[str] = []
        after_ref: str | None = None
        terminal_observed_at = max(
            (item.updated_at for item in task.execution_records), default=utc_now()
        )
        try:
            collector = self._collector(task)
            await collector.start()
            if task.before_snapshot_ref:
                before = writer.load_snapshot(task.before_snapshot_ref)
                after = await collector.wait_for_after(
                    before,
                    terminal_observed_at=terminal_observed_at,
                    timeout_s=self.config.evidence.post_capture_timeout_s,
                )
            else:
                after = await collector.wait_for_before(
                    self.config.evidence.post_capture_timeout_s
                )
            after_ref = writer.write_snapshot("after", after)
        except Exception as exc:
            errors.append(str(exc) or type(exc).__name__)
        finally:
            if collector is not None:
                errors.extend(collector.errors)
                await collector.close()
        bundle, bundle_ref = writer.write_bundle(
            before_ref=task.before_snapshot_ref,
            after_ref=after_ref,
            terminal_observed_at=terminal_observed_at,
            required_sources=list(self.config.evidence.required_image_sources),
            required_kinds=list(task.verification.evidence_policy.required_kinds),
            errors=list(task.evidence_errors) + errors,
        )

        def mutate(current: AgentTaskRecord) -> None:
            current.after_snapshot_ref = after_ref
            current.evidence_bundle_ref = bundle_ref
            current.evidence_errors.extend(errors)

        self.store.update(
            task_id,
            mutate,
            event_type="after_evidence_captured",
            payload={"bundle_id": bundle.bundle_id},
        )

    def _collector(self, task: AgentTaskRecord) -> ForgeObservationCollector:
        gateway_url = (
            task.primary_skill_binding.gateway_url
            if task.primary_skill_binding is not None
            else getattr(self.client, "base_url", None)
        )
        if not isinstance(gateway_url, str) or not gateway_url:
            raise AgentTaskError(
                "Forge evidence collection requires the bound Runtime Gateway URL"
            )
        return ForgeObservationCollector(
            gateway_url,
            required_image_sources=list(self.config.evidence.required_image_sources),
            max_artifact_bytes=self.config.evidence.max_artifact_bytes,
            require_state="robot_state" in task.verification.evidence_policy.required_kinds,
            connection_timeout_s=self.config.evidence.connection_timeout_s,
        )

    def _schedule_experience(self, task: AgentTaskRecord) -> None:
        self.interaction.assert_settled(task.task_id)
        if (
            task.terminal
            and task.primary_skill_binding is not None
            and self.runtime_task_binding_ids is not None
            and not any(
                item.semantics in {"action", "session"} and item.status == "unknown"
                for item in task.execution_records
            )
        ):
            self.runtime_task_binding_ids.discard(task.primary_skill_binding.binding_id)
        if self.experience is not None:
            self.experience.schedule_forge_completion(task.task_id)


def _task_execution(task: AgentTaskRecord, record_id: str) -> ToolExecutionRecord:
    for record in task.execution_records:
        if record.record_id == record_id:
            return record
    raise AgentTaskError(f"Tool execution record not found: {record_id}")


def _owned_execution(
    task: AgentTaskRecord,
    invocation_id: str,
    *,
    semantics: Literal["action", "session"],
) -> ToolExecutionRecord:
    for record in task.execution_records:
        if record.invocation_id == invocation_id and record.semantics == semantics:
            return record
    raise AgentTaskError(
        f"{semantics.title()} invocation {invocation_id!r} does not belong to AgentTask "
        f"{task.task_id!r}"
    )


def _cancel_terminal_status(task: AgentTaskRecord) -> AgentTaskStatus:
    owned = [
        item
        for item in task.execution_records
        if item.semantics == "action"
        or (item.semantics == "session" and item.ownership == "task")
    ]
    if not owned or all(item.status in {"cancelled", "stopped"} for item in owned):
        return AgentTaskStatus.CANCELLED
    return AgentTaskStatus.FAILED


def _execution_facts_succeeded(task: AgentTaskRecord) -> bool:
    return all(
        item.status == "succeeded"
        or (
            item.semantics == "session"
            and (
                (item.ownership == "task" and item.status == "stopped")
                or (
                    item.ownership in {"runtime", "shared"}
                    and item.status in {"accepted", "running", "succeeded", "stopped"}
                )
            )
        )
        for item in task.execution_records
    )


def _remote_identity(exc: Exception) -> tuple[str | None, str | None]:
    payload = getattr(exc, "payload", None)
    data = _response_data(payload) if isinstance(payload, dict) else {}
    invocation_id = data.get("invocation_id")
    attempt_id = data.get("attempt_id")
    return (
        invocation_id if isinstance(invocation_id, str) and invocation_id else None,
        attempt_id if isinstance(attempt_id, str) and attempt_id else None,
    )


def _fail_replan(task: AgentTaskRecord, message: str) -> None:
    task.status = AgentTaskStatus.FAILED
    task.evidence_errors.append(message)


def _response_data(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    return data if isinstance(data, dict) else response


def _tool_status(response: dict[str, Any], *, default: str) -> str:
    data = _response_data(response)
    phase = data.get("phase") or data.get("state")
    if isinstance(phase, str):
        normalized = phase.lower()
        mapping = {
            "dispatching": "pending",
            "accepted": "accepted",
            "running": "running",
            "stopping": "running",
            "completed": "succeeded",
            "succeeded": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "stopped": "stopped",
            "unknown": "unknown",
        }
        if normalized in mapping:
            return mapping[normalized]
    result = data.get("result")
    if data.get("status") == "available" and isinstance(result, dict):
        result_status = result.get("status")
        if isinstance(result_status, str):
            return _tool_status({"phase": result_status}, default=default)
    return default


__all__ = [
    "AgentTaskBusyError",
    "AgentTaskCoordinator",
    "AgentTaskError",
    "AgentTaskRecord",
    "AgentTaskStatus",
    "AgentTaskStore",
    "PlanRevision",
    "TERMINAL_TASK_STATUSES",
    "ToolExecutionRecord",
]
