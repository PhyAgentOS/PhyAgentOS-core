"""Agent-owned task aggregation over the single Forge Tool API execution plane."""

from __future__ import annotations

import asyncio
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

from loguru import logger
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
    """Raised when this session's non-terminal AgentTask slot is occupied.

    The slot is per ``origin_session_key``; an unbound task (``None``) has its
    own slot. ``blocking`` carries the authoritative owner of the busy state:
    the task, the invocation identity the Gateway confirmed (``None`` when
    dispatch was never confirmed), the record status and semantics, and the
    originating session. It never reclassifies or resolves an unknown state.
    """

    def __init__(
        self,
        message: str,
        *,
        task_id: str | None = None,
        invocation_id: str | None = None,
        status: str | None = None,
        blocking: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.invocation_id = invocation_id
        self.status = status
        self.blocking = tuple(dict(item) for item in (blocking or ()))


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
    cancel_requested: bool = False
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    # True only when the Gateway proved the invoke was rejected before anything
    # reached the provider. Such a record documents a tool call that never
    # executed, so task finalization must neither require nor be failed by it.
    rejected_before_dispatch: bool = False
    # Absolute deadline for resolving ``unknown``. Set once when the record first
    # becomes unknown and never extended: a later poll or restart must not keep
    # the task occupied forever.
    unknown_deadline: datetime | None = None
    # True after the deadline passed without a confirmed outcome. The record is
    # then failed local accounting; the remote state stays unconfirmed.
    recovery_expired: bool = False
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
    """Transactional SQLite store enforcing one non-terminal AgentTask per session.

    Unbound tasks (``origin_session_key is None``) share one independent slot.
    Unresolved remote state from another session is retained as a fact but does
    not occupy this session's slot.
    """

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

    def latest_for_session(self, session_key: str) -> AgentTaskRecord | None:
        """Return the most recent task created from one session."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT record_json FROM agent_tasks ORDER BY updated_at DESC"
            ).fetchall()
        for row in rows:
            try:
                record = AgentTaskRecord.model_validate_json(row["record_json"])
            except Exception:
                continue
            if record.origin_session_key == session_key:
                return record
        return None

    def nonterminal_for_session(self, session_key: str) -> AgentTaskRecord | None:
        """Return the newest non-terminal task for one session, if any."""
        tasks = [
            task
            for task in self.active_tasks()
            if task.origin_session_key == session_key
        ]
        if not tasks:
            return None
        return max(tasks, key=lambda task: task.created_at)

    def tasks_with_unknowns(self) -> list[AgentTaskRecord]:
        """Return tasks that still carry an unknown Action/Session invocation."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT record_json FROM agent_tasks "
                "WHERE record_json LIKE '%\"unknown\"%'"
            ).fetchall()
        found: list[AgentTaskRecord] = []
        for row in rows:
            try:
                record = AgentTaskRecord.model_validate_json(row["record_json"])
            except Exception:
                continue
            if any(
                item.status == "unknown"
                and item.semantics in {"action", "session"}
                for item in record.execution_records
            ):
                found.append(record)
        return found

    def has_unresolved_unknowns(self) -> bool:
        """True when any past Action/Session invocation still has unknown remote state."""
        return bool(self.tasks_with_unknowns())

    def unknown_remote_invocations(
        self,
    ) -> list[tuple[AgentTaskRecord, ToolExecutionRecord]]:
        """Return the task and record for every unresolved Action/Session identity."""
        found: list[tuple[AgentTaskRecord, ToolExecutionRecord]] = []
        for task in self.tasks_with_unknowns():
            for record in task.execution_records:
                if (
                    record.status == "unknown"
                    and record.semantics in {"action", "session"}
                ):
                    found.append((task, record))
        return found

    def create(
        self,
        record: AgentTaskRecord,
        *,
        supersede_guard: Callable[[AgentTaskRecord], bool] | None = None,
    ) -> tuple[AgentTaskRecord, list[AgentTaskRecord]]:
        """Insert one task, superseding only locally abandonable same-session work.

        Returns the new record plus snapshots of tasks that were closed locally.
        ``rollback_create`` can delete the replacement and put those snapshots
        back if a later side effect of creating the new task fails.
        """
        superseded: list[AgentTaskRecord] = []
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT record_json FROM agent_tasks "
                "WHERE status NOT IN (?, ?, ?) ORDER BY created_at",
                tuple(item.value for item in TERMINAL_TASK_STATUSES),
            ).fetchall()
            for row in rows:
                try:
                    existing = AgentTaskRecord.model_validate_json(row["record_json"])
                except Exception:
                    continue
                if existing.origin_session_key != record.origin_session_key:
                    continue
                if _stale_dispatch_unknown(existing) and (
                    supersede_guard is None or supersede_guard(existing)
                ):
                    # The only unfinished work is an Action/Session whose dispatch
                    # outcome was never confirmed and has no Gateway identity. Close
                    # the stale task locally as failed, keep the execution records
                    # unknown, and let this session start a genuinely new task. The
                    # remote state is not reclassified.
                    superseded.append(existing.model_copy(deep=True))
                    self._supersede_locally(connection, existing, record.task_id)
                    continue
                scope = (
                    "the unbound slot"
                    if record.origin_session_key is None
                    else f"session {record.origin_session_key!r}"
                )
                raise AgentTaskBusyError(
                    f"AgentTask {existing.task_id} is still non-terminal "
                    f"(status={existing.status.value}); finish or cancel it before "
                    f"creating another AgentTask in {scope}",
                    task_id=existing.task_id,
                    status=existing.status.value,
                    blocking=[
                        {
                            "task_id": existing.task_id,
                            "invocation_id": None,
                            "status": existing.status.value,
                            "semantics": "task",
                            "origin_session_key": existing.origin_session_key,
                        }
                    ],
                )
            self._insert(connection, record)
            self._event(connection, record.task_id, "task_created", {})
            connection.commit()
        return record, superseded

    def rollback_create(
        self,
        replacement_task_id: str,
        snapshots: list[AgentTaskRecord],
    ) -> list[str]:
        """Delete a failed replacement and restore local superseded task state.

        Both steps run in one transaction so no other writer can create a
        same-session task between them. Restore is deliberately narrow: only
        the task status, ``terminal_at`` and the supersede marker are changed;
        execution records and other facts written after supersession stay as
        they are. If another non-terminal task already exists for that session,
        the old task stays failed with an explanatory evidence entry.
        """
        restored: list[str] = []
        marker = (
            f"superseded locally by AgentTask {replacement_task_id}; "
            "the remote Action/Session state remains unknown"
        )
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM agent_task_events WHERE task_id = ?",
                (replacement_task_id,),
            )
            connection.execute(
                "DELETE FROM agent_tasks WHERE task_id = ?",
                (replacement_task_id,),
            )
            for original in snapshots:
                row = connection.execute(
                    "SELECT record_json FROM agent_tasks WHERE task_id = ?",
                    (original.task_id,),
                ).fetchone()
                if row is None:
                    continue
                current = AgentTaskRecord.model_validate_json(row["record_json"])
                if current.status != AgentTaskStatus.FAILED or not any(
                    marker in item for item in current.evidence_errors
                ):
                    continue
                current.evidence_errors = [
                    item for item in current.evidence_errors if marker not in item
                ]
                current.updated_at = utc_now()
                if self._session_has_other_active(
                    connection, current.origin_session_key, current.task_id
                ):
                    current.evidence_errors.append(
                        "supersede rollback skipped: another AgentTask for this "
                        "session is non-terminal"
                    )
                    event_type = "task_supersede_rollback_skipped"
                else:
                    current.status = original.status
                    current.terminal_at = original.terminal_at
                    event_type = "task_supersede_rolled_back"
                connection.execute(
                    "UPDATE agent_tasks SET status = ?, record_json = ?, updated_at = ? "
                    "WHERE task_id = ?",
                    (
                        current.status.value,
                        current.model_dump_json(),
                        current.updated_at.isoformat(),
                        current.task_id,
                    ),
                )
                self._event(
                    connection,
                    current.task_id,
                    event_type,
                    {"replacement_task_id": replacement_task_id},
                )
                if event_type == "task_supersede_rolled_back":
                    restored.append(current.task_id)
            connection.commit()
        return restored

    @staticmethod
    def _session_has_other_active(
        connection: sqlite3.Connection,
        origin_session_key: str | None,
        exclude_task_id: str,
    ) -> bool:
        rows = connection.execute(
            "SELECT record_json FROM agent_tasks "
            "WHERE task_id != ? AND status NOT IN (?, ?, ?)",
            (
                exclude_task_id,
                *(item.value for item in TERMINAL_TASK_STATUSES),
            ),
        ).fetchall()
        for row in rows:
            try:
                task = AgentTaskRecord.model_validate_json(row["record_json"])
            except Exception:
                continue
            if task.origin_session_key == origin_session_key:
                return True
        return False

    @staticmethod
    def _supersede_locally(
        connection: sqlite3.Connection,
        existing: AgentTaskRecord,
        new_task_id: str,
    ) -> None:
        message = (
            f"superseded locally by AgentTask {new_task_id}; the remote "
            "Action/Session state remains unknown"
        )
        existing.status = AgentTaskStatus.FAILED
        existing.evidence_errors.append(message)
        existing.updated_at = utc_now()
        existing.terminal_at = existing.updated_at
        connection.execute(
            "UPDATE agent_tasks SET status = ?, record_json = ?, updated_at = ? "
            "WHERE task_id = ?",
            (
                existing.status.value,
                existing.model_dump_json(),
                existing.updated_at.isoformat(),
                existing.task_id,
            ),
        )
        AgentTaskStore._event(
            connection,
            existing.task_id,
            "task_superseded_locally",
            {"reason": message, "superseded_by": new_task_id},
        )

    def get(self, task_id: str) -> AgentTaskRecord:
        with self._lock, self._connection() as connection:
            return self._get(connection, task_id)

    def delete(self, task_id: str) -> None:
        """Remove a task created during a failed bind; not a lifecycle transition."""
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM agent_task_events WHERE task_id = ?", (task_id,)
            )
            connection.execute("DELETE FROM agent_tasks WHERE task_id = ?", (task_id,))
            connection.commit()

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

    def active_tasks(self) -> list[AgentTaskRecord]:
        """Return every real non-terminal AgentTask across all sessions."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT record_json FROM agent_tasks WHERE status NOT IN (?, ?, ?) "
                "ORDER BY created_at",
                tuple(item.value for item in TERMINAL_TASK_STATUSES),
            ).fetchall()
        tasks: list[AgentTaskRecord] = []
        for row in rows:
            try:
                tasks.append(AgentTaskRecord.model_validate_json(row["record_json"]))
            except Exception:
                continue
        return tasks

    def active(self) -> AgentTaskRecord | None:
        tasks = self.active_tasks()
        return tasks[0] if tasks else None

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
        cancel_grace_s: float = 0.75,
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
        self.cancel_timeout_s = max(0.1, float(config.request_timeout_s))
        self.cancel_poll_interval_s = max(0.01, float(config.poll_interval_s))
        self.cancel_grace_s = max(0.01, float(cancel_grace_s))

    def set_experience(self, experience: Any | None) -> None:
        self.experience = experience

    def set_activation_manager(self, activation_manager: Any) -> None:
        self.activation_manager = activation_manager

    def _can_supersede_locally(self, task: AgentTaskRecord) -> bool:
        """Never close an interaction-managed task behind the supervisor's back."""
        try:
            return not self.interaction.managed(task.task_id)
        except Exception as exc:
            logger.warning(
                "could not determine interaction ownership for {}: {}; keeping it",
                task.task_id,
                exc,
            )
            return False

    def _unknown_recovery_timeout(self) -> float:
        return float(getattr(self.config, "unknown_recovery_timeout_s", 3600.0))

    def expire_timed_out_unknowns(self) -> list[str]:
        """Settle ``unknown`` executions whose recovery deadline has passed.

        A record that stays unknown past its deadline cannot be confirmed by
        PAOS. It is failed as local accounting with a ``RecoveryTimeout`` error:
        the session slot is released, but the remote outcome stays unconfirmed
        and the request is never resent. A task with no live or unresolved
        owned execution closes as failed at the same time.

        The deadline is recorded once when the record first becomes unknown and
        is never extended by polling or a restart. Legacy records without a
        deadline fall back to ``updated_at`` so an already old unknown does not
        get a fresh hour.
        """
        timeout_s = self._unknown_recovery_timeout()
        now = utc_now()
        expired: list[str] = []
        for task in self.store.active_tasks():
            try:
                expired.extend(self._expire_task_unknowns(task.task_id, now, timeout_s))
            except AgentTaskError:
                # A concurrent rollback or delete removed the row; nothing to settle.
                continue
        return expired

    def _expire_task_unknowns(
        self, task_id: str, now: datetime, timeout_s: float
    ) -> list[str]:
        """Expire one task's due unknowns; the caller tolerates concurrent deletion."""
        task = self.store.get(task_id)
        if any(
            record.status == "unknown" and record.unknown_deadline is None
            for record in task.execution_records
        ):
            def stamp(current: AgentTaskRecord) -> None:
                for record in current.execution_records:
                    if record.status == "unknown" and record.unknown_deadline is None:
                        record.unknown_deadline = record.updated_at + timedelta(
                            seconds=timeout_s
                        )

            self.store.update(
                task_id,
                stamp,
                event_type="tool_execution_recovery_deadline_recorded",
            )

        task = self.store.get(task_id)
        due = [
            record.record_id
            for record in task.execution_records
            if record.status == "unknown"
            and record.unknown_deadline is not None
            and record.unknown_deadline <= now
        ]
        if not due:
            return []

        def fail(current: AgentTaskRecord, due_ids: tuple[str, ...] = tuple(due)) -> None:
            for record in current.execution_records:
                if record.record_id not in due_ids:
                    continue
                record.status = "failed"
                record.recovery_expired = True
                record.error = {
                    "type": "RecoveryTimeout",
                    "message": (
                        "recovery deadline passed without a confirmed Gateway outcome; "
                        "this execution is failed locally and was not resent"
                    ),
                    "deadline": record.unknown_deadline.isoformat()
                    if record.unknown_deadline is not None
                    else None,
                }
                record.updated_at = now
                current.evidence_errors.append(
                    f"Tool execution {record.record_id} ({record.tool_id}) recovery "
                    "expired: the remote outcome stays unconfirmed"
                )
            current.updated_at = now

        updated = self.store.update(
            task_id,
            fail,
            event_type="tool_execution_recovery_expired",
            payload={"record_ids": list(due)},
        )

        if (
            updated.status == AgentTaskStatus.EXECUTING
            and any(item.recovery_expired for item in _owned_executions(updated))
            and not any(
                item.status in {"pending", "accepted", "running", "unknown"}
                for item in _owned_executions(updated)
            )
        ):
            def close(current: AgentTaskRecord) -> None:
                if current.status != AgentTaskStatus.EXECUTING:
                    return
                current.status = AgentTaskStatus.FAILED
                current.terminal_at = now
                current.updated_at = now
                current.evidence_errors.append(
                    "AgentTask failed locally because every unfinished execution "
                    "passed its recovery deadline without a confirmed outcome"
                )

            self.store.update(
                task_id,
                close,
                event_type="task_recovery_expired",
            )
            try:
                managed = self.interaction.managed(task_id)
            except Exception:
                managed = True
            if not managed:
                try:
                    self._schedule_experience(self.store.get(task_id))
                except Exception as exc:  # maintenance must not fail the caller
                    logger.warning(
                        "could not schedule experience after task {} recovery expiry: {}",
                        task_id,
                        exc,
                    )
        return due

    def unknown_remote_blockers(self) -> list[dict[str, Any]]:
        """Describe unresolved Action/Session records as read-only facts.

        This is diagnostic state only: it no longer gates AgentTask creation
        from other sessions or the same session after local supersession.
        """
        return [
            {
                "task_id": task.task_id,
                "invocation_id": record.invocation_id,
                "status": record.status,
                "semantics": record.semantics,
                "origin_session_key": task.origin_session_key,
            }
            for task, record in self.store.unknown_remote_invocations()
        ]

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
        # Release slot occupied only by executions whose recovery deadline
        # passed before admitting a new task.
        self.expire_timed_out_unknowns()
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
        task, _superseded = self.store.create(
            task, supersede_guard=self._can_supersede_locally
        )
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
        self.expire_timed_out_unknowns()
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
                raise AgentTaskError(
                    "primary Skill activation has no Forge binding candidate; "
                    "only an installed Forge Skill with a ready Runtime can create "
                    "an AgentTask"
                )
            try:
                binding = await self.binding_resolver.freeze(
                    candidate_id,
                    task_id=task_id,
                    activated_document_sha256=activation.content_sha256,
                )
            except ForgeSkillBindingError as exc:
                raise AgentTaskError(str(exc)) from exc
            if (
                binding.workflow_document_sha256 is not None
                and binding.workflow_document_sha256 != binding.skill_document_sha256
            ):
                logger.warning(
                    "workflow document sha256 {} differs from the installed bundle "
                    "document sha256 {}; proceeding with the locally modified document",
                    binding.workflow_document_sha256[:12],
                    binding.skill_document_sha256[:12],
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
        # Reserve this session's slot before writing any interaction binding.
        # A busy slot must not leave journal state behind for a task that was
        # never created.
        superseded: list[AgentTaskRecord] = []
        try:
            task, superseded = self.store.create(
                task, supersede_guard=self._can_supersede_locally
            )
            await self.interaction.freeze(task)
            if binding is not None and self.runtime_task_binding_ids is not None:
                self.runtime_task_binding_ids.add(binding.binding_id)
            if self.interaction.path.exists() and self.interaction.store.binding(task_id):
                self.store.update(task_id, lambda current: None, event_type="interaction_bound")
        except Exception:
            if binding is not None and self.runtime_task_binding_ids is not None:
                self.runtime_task_binding_ids.discard(binding.binding_id)
            self._discard_interaction_binding(task_id)
            self.store.rollback_create(task_id, superseded)
            raise
        if self.experience is not None and origin_session_key:
            self.experience.bind_forge_task(
                task_id,
                session_key=origin_session_key,
                forge_binding=binding,
            )
        return task

    def _discard_interaction_binding(self, task_id: str) -> None:
        """Delete a journal binding written before a bind failure rolled back."""
        store = self.interaction._store
        if store is None:
            if not self.interaction.path.exists():
                return
            store = self.interaction.store
        try:
            store.db.execute(
                "DELETE FROM interaction_bindings WHERE task_id = ?", (task_id,)
            )
        except sqlite3.OperationalError:
            pass  # no binding table exists, so nothing was written

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
        wait_ms: int | None = None,
    ) -> dict[str, Any]:
        if wait_ms is not None:
            if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or wait_ms < 0:
                raise ValueError("wait_ms must be a non-negative int or None")
            wait_ms = min(wait_ms, 60_000)
        task = self._require_executable(task_id)
        if any(
            item.semantics == "action"
            and item.tool_id == tool_id
            and item.arguments == arguments
            and (item.status == "unknown" or item.recovery_expired)
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
        self._assert_dispatch_window(task_id, record_id)
        client = self._pinned_execution_client(task_id)
        invocation_id: str | None = None
        attempt_id: str | None = None
        inner = asyncio.ensure_future(
            client.invoke_action(
                tool_id, arguments, caller_id=caller, timeout_ms=timeout_ms
            )
        )
        try:
            response = await asyncio.shield(inner)
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
        except asyncio.CancelledError as exc:
            response, failure = await self._dispatch_after_cancel(inner)
            remote_invocation_id, remote_attempt_id = _remote_identity(exc)
            if response is not None:
                data = _response_data(response)
                response_id = data.get("invocation_id")
                response_attempt = data.get("attempt_id")
                if isinstance(response_id, str) and response_id:
                    remote_invocation_id = response_id
                if isinstance(response_attempt, str) and response_attempt:
                    remote_attempt_id = response_attempt
            if failure is not None:
                failed_id, failed_attempt = _remote_identity(failure)
                remote_invocation_id = remote_invocation_id or failed_id
                remote_attempt_id = remote_attempt_id or failed_attempt
            invocation_id = remote_invocation_id or invocation_id
            attempt_id = remote_attempt_id or attempt_id
            # A structured rejection from the grace window never reached a
            # provider, so it must not stay unknown or arm the resend guard.
            rejected_before_dispatch = (
                failure is not None and _rejected_before_dispatch(failure)
            )
            self._finish_execution(
                task_id,
                record_id,
                status="failed" if rejected_before_dispatch else "unknown",
                invocation_id=invocation_id,
                attempt_id=attempt_id,
                response=response,
                rejected_before_dispatch=rejected_before_dispatch,
                error={
                    "type": (
                        type(failure).__name__
                        if rejected_before_dispatch and failure is not None
                        else "CancelledError"
                    ),
                    "message": (
                        str(failure)
                        if rejected_before_dispatch and failure is not None
                        else (
                            "Action dispatch was cancelled; remote state is unknown and "
                            "the request was not resent"
                            + (
                                ""
                                if invocation_id
                                else "; Gateway invocation identity could not be confirmed"
                            )
                        )
                    ),
                },
            )
            if invocation_id and self.runtime_invocation_ids is not None:
                self._track_remote_identity(
                    task_id,
                    record_id,
                    invocation_id,
                    tracker=self.runtime_invocation_ids,
                    response={
                        "ok": False,
                        "error": {
                            "type": "CancelledError",
                            "message": "dispatch cancelled; remote state is unknown",
                        },
                    },
                    kind="Action",
                )
            if invocation_id:
                record = _task_execution(self.store.get(task_id), record_id)
                await self._request_remote_stop(task_id, record, client=client)
                self._settle_cancellation(task_id)
            raise
        except Exception as exc:
            remote_invocation_id, remote_attempt_id = _remote_identity(exc)
            invocation_id = remote_invocation_id or invocation_id
            attempt_id = remote_attempt_id or attempt_id
            # A structured pre-dispatch rejection leaves no remote state, so
            # the record is definitively failed: the identical-Action guard
            # must not arm and block a later identical resend.
            rejected_before_dispatch = _rejected_before_dispatch(exc)
            error: dict[str, Any] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            for name in ("status_code", "error_code", "retryable"):
                value = getattr(exc, name, None)
                if value is not None:
                    error[name] = value
            self._finish_execution(
                task_id,
                record_id,
                status="failed" if rejected_before_dispatch else "unknown",
                invocation_id=invocation_id,
                attempt_id=attempt_id,
                error=error,
                rejected_before_dispatch=rejected_before_dispatch,
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
        current = self.store.get(task_id)
        accepted_record = _task_execution(current, record_id)
        if (
            current.cancellation_requested
            or current.status != AgentTaskStatus.EXECUTING
        ) and not accepted_record.cancel_requested:
            await self._request_remote_stop(task_id, accepted_record, client=client)
            self._settle_cancellation(task_id)
        if (
            wait_ms
            and not current.cancellation_requested
            and current.status == AgentTaskStatus.EXECUTING
        ):
            response = await self._wait_action_result(
                task_id,
                record_id,
                invocation_id,
                client,
                response,
                wait_ms,
            )
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
        timeout_s = self._unknown_recovery_timeout()

        def mutate(current: AgentTaskRecord) -> None:
            target = _task_execution(current, record.record_id)
            target.status = status
            if status == "unknown":
                _stamp_unknown_deadline(target, timeout_s)
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
            target.cancel_requested = True
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
        self._assert_dispatch_window(task_id, record_id)
        client = self._execution_client(task_id)
        invocation_id: str | None = None
        attempt_id: str | None = None
        inner = asyncio.ensure_future(
            client.start_session(tool_id, arguments, caller_id=caller)
        )
        try:
            response = await asyncio.shield(inner)
            data = _response_data(response)
            invocation_id = data.get("invocation_id")
            attempt_id = data.get("attempt_id")
            if not isinstance(invocation_id, str) or not invocation_id:
                raise AgentTaskError("Gateway Session response omitted invocation_id")
            if not isinstance(attempt_id, str):
                attempt_id = None
        except asyncio.CancelledError as exc:
            response, failure = await self._dispatch_after_cancel(inner)
            remote_invocation_id, remote_attempt_id = _remote_identity(exc)
            if response is not None:
                data = _response_data(response)
                response_id = data.get("invocation_id")
                response_attempt = data.get("attempt_id")
                if isinstance(response_id, str) and response_id:
                    remote_invocation_id = response_id
                if isinstance(response_attempt, str) and response_attempt:
                    remote_attempt_id = response_attempt
            if failure is not None:
                failed_id, failed_attempt = _remote_identity(failure)
                remote_invocation_id = remote_invocation_id or failed_id
                remote_attempt_id = remote_attempt_id or failed_attempt
            invocation_id = remote_invocation_id or invocation_id
            attempt_id = remote_attempt_id or attempt_id
            rejected_before_dispatch = (
                failure is not None and _rejected_before_dispatch(failure)
            )
            self._finish_execution(
                task_id,
                record_id,
                status="failed" if rejected_before_dispatch else "unknown",
                invocation_id=invocation_id,
                attempt_id=attempt_id,
                response=response,
                rejected_before_dispatch=rejected_before_dispatch,
                error={
                    "type": (
                        type(failure).__name__
                        if rejected_before_dispatch and failure is not None
                        else "CancelledError"
                    ),
                    "message": (
                        str(failure)
                        if rejected_before_dispatch and failure is not None
                        else (
                            "Session start was cancelled; remote state is unknown and "
                            "the request was not resent"
                            + (
                                ""
                                if invocation_id
                                else "; Gateway invocation identity could not be confirmed"
                            )
                        )
                    ),
                },
            )
            if invocation_id and self.runtime_session_ids is not None:
                self._track_remote_identity(
                    task_id,
                    record_id,
                    invocation_id,
                    tracker=self.runtime_session_ids,
                    response={
                        "ok": False,
                        "error": {
                            "type": "CancelledError",
                            "message": "dispatch cancelled; remote state is unknown",
                        },
                    },
                    kind="Session",
                )
            if invocation_id:
                record = _task_execution(self.store.get(task_id), record_id)
                await self._request_remote_stop(task_id, record, client=client)
                self._settle_cancellation(task_id)
            raise
        except Exception as exc:
            remote_invocation_id, remote_attempt_id = _remote_identity(exc)
            invocation_id = remote_invocation_id or invocation_id
            attempt_id = remote_attempt_id or attempt_id
            rejected_before_dispatch = _rejected_before_dispatch(exc)
            self._finish_execution(
                task_id,
                record_id,
                status="failed" if rejected_before_dispatch else "unknown",
                invocation_id=invocation_id,
                attempt_id=attempt_id,
                error={"type": type(exc).__name__, "message": str(exc)},
                rejected_before_dispatch=rejected_before_dispatch,
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
        current = self.store.get(task_id)
        accepted_record = _task_execution(current, record_id)
        if (
            current.cancellation_requested
            or current.status != AgentTaskStatus.EXECUTING
        ) and not accepted_record.cancel_requested:
            await self._request_remote_stop(task_id, accepted_record, client=client)
            self._settle_cancellation(task_id)
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
        timeout_s = self._unknown_recovery_timeout()

        def mutate(current: AgentTaskRecord) -> None:
            target = _task_execution(current, record.record_id)
            target.status = status
            if status == "unknown":
                _stamp_unknown_deadline(target, timeout_s)
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
        if task.terminal and not _has_cancel_retry(task):
            return task

        reason_text = reason.strip()

        def mark(current: AgentTaskRecord) -> None:
            if current.terminal and not _has_cancel_retry(current):
                return
            current.cancellation_requested = True
            current.evidence_errors.append(f"task cancellation requested: {reason_text}")
            current.status = (
                AgentTaskStatus.CANCELLING
                if _has_owned_execution(current) or _has_cancel_retry(current)
                else AgentTaskStatus.CANCELLED
            )

        # The durable gate closes before any await, so no new Action can be admitted
        # while remote cancel/stop requests are in flight.
        task = self.store.update(task_id, mark, event_type="task_cancel_requested")
        if task.terminal and not _has_cancel_retry(task):
            self._schedule_experience(task)
            return task

        pending = [
            item
            for item in task.execution_records
            if (
                item.semantics == "action"
                or (item.semantics == "session" and item.ownership == "task")
            )
            and item.invocation_id
            and (not item.cancel_requested or _needs_cancel_retry(item))
            and (not item.terminal or item.status == "unknown")
        ]
        if pending:
            client = self._execution_client(task_id)
            # Independent bounded stops run concurrently so several invocations do not
            # serialize one full Gateway timeout each.
            await asyncio.gather(
                *(
                    self._request_remote_stop(task_id, item, client=client)
                    for item in pending
                )
            )

        def finish(current: AgentTaskRecord) -> None:
            if current.terminal:
                return
            current.status = (
                AgentTaskStatus.CANCELLING
                if _has_nonterminal_owned_execution(current)
                else _cancel_terminal_status(current)
            )

        result = self.store.update(task_id, finish, event_type="task_cancel_dispatched")
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
        """Repair every real non-terminal task from Gateway GETs, never POSTs.

        Terminal tasks are history: their unknown records stay unknown and are
        not re-queried here. Creation no longer depends on clearing them.
        """
        if self.interaction.supervisor is not None:
            await self.interaction.supervisor.recover()
        tasks = self.store.active_tasks()
        for task in tasks:
            if self.interaction.managed(task.task_id):
                continue  # the interaction supervisor recovered its own runs
            await self._reconcile_task(task)
        # Only after the GET-based recovery attempt: an execution still unknown
        # past its deadline is failed locally so it cannot hold the slot forever.
        self.expire_timed_out_unknowns()
        remaining = self.store.active_tasks()
        return remaining[0] if remaining else None

    async def _reconcile_task(self, task: AgentTaskRecord) -> None:
        binding = task.primary_skill_binding
        if binding is not None and self.runtime_task_binding_ids is not None:
            self.runtime_task_binding_ids.add(binding.binding_id)
        for record in task.execution_records:
            if record.semantics == "query":
                continue
            unknown_with_identity = (
                record.status == "unknown" and bool(record.invocation_id)
            )
            if record.terminal and not unknown_with_identity:
                continue
            if not record.invocation_id:
                if record.status == "unknown":
                    # Keep the original dispatch-uncertain fact and its error.
                    continue
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
            if unknown_with_identity:
                await self._reconcile_unknown(task.task_id, record)
                continue
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

    async def _reconcile_unknown(
        self, task_id: str, record: ToolExecutionRecord
    ) -> None:
        """Confirm one unknown record with a GET only; never redispatch a POST."""
        assert record.invocation_id is not None
        try:
            response = await self._execution_client(task_id).invocation_status(
                record.invocation_id
            )
        except Exception:
            return
        status = _tool_status(response, default="unknown")
        if status in {"cancelled", "stopped", "failed", "succeeded"}:
            self._finish_execution(
                task_id, record.record_id, status=status, response=response
            )

    def _pinned_execution_client(self, task_id: str):
        """Return one concrete client for an entire Action admission and wait.

        ``DynamicForgeToolClient`` follows the active runtime on every attribute
        access. A dispatch and its result reads must not land on two different
        Gateways, so the active client is resolved once here.
        """
        from PhyAgentOS.forge.interaction.client import trusted_runtime

        runtime = trusted_runtime(self, task_id)
        if runtime is not None:
            return runtime.client
        client = self.client
        registry = getattr(client, "registry", None)
        current = getattr(registry, "current", None)
        if not callable(current):
            return client
        active = current()
        if active is None:
            raise AgentTaskError("no ready Forge Skill runtime is active")
        pinned = getattr(active, "client", None)
        if pinned is None:
            raise AgentTaskError("the active Forge Skill runtime has no Gateway client")
        return pinned

    async def _wait_action_result(
        self,
        task_id: str,
        record_id: str,
        invocation_id: str,
        client,
        accepted_response: dict[str, Any],
        wait_ms: int,
    ) -> dict[str, Any]:
        """Poll one accepted Action until it is terminal or the wait expires.

        A terminal response is returned to the tool adapter, which maps it to
        success or failure. A timeout returns the last pending response and
        leaves the record non-terminal; the caller can still reconcile it later.
        """
        if wait_ms <= 0:
            return accepted_response
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (wait_ms / 1000.0)
        last = accepted_response
        last_status = "accepted"
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return last
            current = self.store.get(task_id)
            if current.terminal or current.cancellation_requested:
                return last
            if _task_execution(current, record_id).terminal:
                return last

            try:
                response = await asyncio.wait_for(
                    client.invocation_result(invocation_id), timeout=remaining
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A read failure does not turn the admitted Action into a
                # success or failure. Keep the last known response.
                return last
            # Result envelopes may omit the admission identities. Keep them
            # visible so the model can reconcile a pending Action later.
            data = dict(_response_data(response))
            admitted = _response_data(accepted_response)
            for identity in ("invocation_id", "attempt_id"):
                if identity in admitted:
                    data.setdefault(identity, admitted[identity])
            response = {**response, "data": data}
            last = response
            status = _tool_status(response, default="accepted")
            if status != last_status or status in TERMINAL_TOOL_STATUSES:
                self.observe_action(task_id, invocation_id, response)
                last_status = status
            if status in TERMINAL_TOOL_STATUSES:
                return response

            remaining = deadline - loop.time()
            if remaining <= 0:
                return last
            await asyncio.sleep(min(self.cancel_poll_interval_s, remaining))

    def _execution_client(self, task_id):
        from PhyAgentOS.forge.interaction.client import trusted_runtime
        runtime = trusted_runtime(self, task_id)
        return runtime.client if runtime is not None else self.client

    async def cancel_action(self, task_id: str, invocation_id: str):
        record = self.require_action_invocation(task_id, invocation_id)
        if self.interaction.managed(task_id):
            await self.interaction.cancel(task_id, "action_cancel_requested")
            return {"ok": True, "data": {"status": "requested"}}
        if not self._claim_cancel_request(task_id, record.record_id):
            return {"ok": True, "data": {"status": "already_requested"}}
        response = await asyncio.wait_for(
            self._execution_client(task_id).cancel_invocation(invocation_id),
            timeout=self.cancel_timeout_s,
        )
        self.record_cancel_response(task_id, invocation_id, response)
        return response

    def capabilities_summary(self) -> str:
        return (
            "Forge execution uses only an activated Skill, a frozen AgentTask binding, and the "
            "Gateway Tool API. Query is read-only; Action admission is not task success; "
            "task-owned Sessions must be stopped before finalization."
        )

    async def _dispatch_after_cancel(
        self, inner: asyncio.Future
    ) -> tuple[dict[str, Any] | None, BaseException | None]:
        """Give an interrupted dispatch a short grace period to reveal its 202 identity."""
        response: dict[str, Any] | None = None
        failure: BaseException | None = None
        try:
            response = await asyncio.wait_for(
                asyncio.shield(inner), timeout=self.cancel_grace_s
            )
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            failure = exc
        if response is None and inner.done() and not inner.cancelled():
            try:
                response = inner.result()
                failure = None
            except Exception as exc:
                failure = exc
        if response is None and not inner.done():
            inner.cancel()
        return response, failure

    async def _request_remote_stop(
        self, task_id: str, record: ToolExecutionRecord, *, client: Any
    ) -> None:
        if not self._claim_cancel_request(task_id, record.record_id):
            return
        invocation_id = record.invocation_id
        assert invocation_id is not None
        loop = asyncio.get_running_loop()
        error: dict[str, Any] | None = None
        try:
            call = (
                client.stop_session(invocation_id)
                if record.semantics == "session"
                else client.cancel_invocation(invocation_id)
            )
            response = await asyncio.wait_for(call, timeout=self.cancel_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            response = {"ok": False, "error": error}
        self._mark_cancel_response(
            task_id,
            record.record_id,
            response,
            error=error,
            semantics=record.semantics,
        )
        await self._await_cancel_terminal(
            task_id,
            record,
            client=client,
            deadline=loop.time() + self.cancel_timeout_s,
        )

    async def _await_cancel_terminal(
        self,
        task_id: str,
        record: ToolExecutionRecord,
        *,
        client: Any,
        deadline: float,
    ) -> None:
        invocation_id = record.invocation_id
        assert invocation_id is not None
        loop = asyncio.get_running_loop()
        terminal_phases = {"cancelled", "stopped", "failed", "succeeded"}
        last_error: Exception | None = None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                response = await asyncio.wait_for(
                    client.invocation_status(invocation_id), timeout=remaining
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _retryable_poll_error(exc):
                    self._mark_cancel_unconfirmed(task_id, record.record_id, exc)
                    return
                last_error = exc
                await asyncio.sleep(
                    min(self.cancel_poll_interval_s, max(0.0, deadline - loop.time()))
                )
                continue
            status = _tool_status(response, default="unknown")
            if status in terminal_phases:
                self._finish_execution(
                    task_id, record.record_id, status=status, response=response
                )
                return

            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                result = await asyncio.wait_for(
                    client.invocation_result(invocation_id), timeout=remaining
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _retryable_poll_error(exc):
                    self._mark_cancel_unconfirmed(task_id, record.record_id, exc)
                    return
                last_error = exc
                await asyncio.sleep(
                    min(self.cancel_poll_interval_s, max(0.0, deadline - loop.time()))
                )
                continue
            status = _tool_status(result, default="unknown")
            if status in terminal_phases:
                self._finish_execution(
                    task_id, record.record_id, status=status, response=result
                )
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self.cancel_poll_interval_s, remaining))
        self._mark_cancel_unconfirmed(task_id, record.record_id, last_error)

    def _claim_cancel_request(self, task_id: str, record_id: str) -> bool:
        """Durably mark a record before the cancel/stop POST so retries do not resend."""
        claimed = False

        def mutate(current: AgentTaskRecord) -> None:
            nonlocal claimed
            target = _task_execution(current, record_id)
            if target.cancel_requested and not _needs_cancel_retry(target):
                return
            retrying = _needs_cancel_retry(target)
            target.cancel_requested = True
            if retrying:
                # A repeated stop is safe and is the only way to recover from a
                # POST that failed after the first attempt was recorded.
                target.error = None
            target.updated_at = utc_now()
            claimed = True

        self.store.update(task_id, mutate, event_type="tool_cancel_requested")
        return claimed

    def _mark_cancel_response(
        self,
        task_id: str,
        record_id: str,
        response: dict[str, Any],
        *,
        error: dict[str, Any] | None = None,
        semantics: Literal["query", "action", "session"],
    ) -> None:
        def mutate(current: AgentTaskRecord) -> None:
            target = _task_execution(current, record_id)
            target.cancel_requested = True
            target.response = response
            if error is not None:
                target.error = error
            target.updated_at = utc_now()

        event_type = (
            "session_stop_requested" if semantics == "session" else "action_cancel_requested"
        )
        self.store.update(task_id, mutate, event_type=event_type)

    def _settle_cancellation(self, task_id: str) -> AgentTaskRecord:
        changed = False

        def mutate(current: AgentTaskRecord) -> None:
            nonlocal changed
            if current.terminal or not current.cancellation_requested:
                return
            current.status = (
                AgentTaskStatus.CANCELLING
                if _has_nonterminal_owned_execution(current)
                else _cancel_terminal_status(current)
            )
            changed = True

        result = self.store.update(task_id, mutate, event_type="task_cancel_settled")
        if changed and result.terminal:
            self._schedule_experience(result)
        return result

    def _mark_cancel_unconfirmed(
        self, task_id: str, record_id: str, exc: Exception | None = None
    ) -> None:
        message = (
            "cancellation was requested but Gateway did not confirm a terminal state; "
            "cannot confirm remote state and the request was not resent"
        )
        error: dict[str, Any] = {"type": "CancellationUnconfirmed", "message": message}
        if exc is not None:
            error["cause"] = {"type": type(exc).__name__, "message": str(exc)}
        timeout_s = self._unknown_recovery_timeout()

        def mutate(current: AgentTaskRecord) -> None:
            target = _task_execution(current, record_id)
            if target.terminal and target.status != "unknown":
                return
            target.status = "unknown"
            _stamp_unknown_deadline(target, timeout_s)
            target.error = error
            target.updated_at = utc_now()

        self.store.update(task_id, mutate, event_type="tool_execution_finished")

    def _assert_dispatch_window(self, task_id: str, record_id: str) -> None:
        """Re-check the durable gate after intent is recorded and before any POST."""
        task = self.store.get(task_id)
        if task.cancellation_requested or task.status != AgentTaskStatus.EXECUTING:
            message = (
                f"AgentTask {task_id} stopped accepting dispatch before the Gateway "
                "request was sent; remote state is unchanged and the request was not resent"
            )
            aborted_status = (
                "cancelled"
                if task.cancellation_requested or task.status == AgentTaskStatus.CANCELLED
                else "failed"
            )
            self._finish_execution(
                task_id,
                record_id,
                status=aborted_status,
                error={"type": "DispatchAborted", "message": message},
                rejected_before_dispatch=True,
            )
            raise AgentTaskError(message)

    def _require_executable(self, task_id: str) -> AgentTaskRecord:
        task = self.store.get(task_id)
        if task.cancellation_requested:
            raise AgentTaskError(
                f"AgentTask {task_id} is not accepting Tool calls: cancellation was requested"
            )
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
        rejected_before_dispatch: bool = False,
    ) -> None:
        timeout_s = self._unknown_recovery_timeout()

        def mutate(current: AgentTaskRecord) -> None:
            record = _task_execution(current, record_id)
            record.status = status  # type: ignore[assignment]
            record.invocation_id = invocation_id or record.invocation_id
            record.attempt_id = attempt_id or record.attempt_id
            record.response = response
            record.error = error
            record.rejected_before_dispatch = rejected_before_dispatch
            if status == "unknown":
                _stamp_unknown_deadline(record, timeout_s)
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
            record = _task_execution(self.store.get(task_id), record_id)
            if record.terminal and record.status != "unknown":
                # A terminal observation already removed this identity from
                # the Runtime tracker; admission must not add it back. An
                # unknown result stays tracked for later reconciliation.
                return response
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


def _has_owned_execution(task: AgentTaskRecord) -> bool:
    return any(
        item.semantics == "action"
        or (item.semantics == "session" and item.ownership == "task")
        for item in task.execution_records
    )


def _owned_executions(task: AgentTaskRecord) -> list[ToolExecutionRecord]:
    return [
        item
        for item in task.execution_records
        if item.semantics == "action"
        or (item.semantics == "session" and item.ownership == "task")
    ]


def _stamp_unknown_deadline(
    record: ToolExecutionRecord,
    timeout_s: float,
    *,
    now: datetime | None = None,
) -> None:
    """Mark when an ``unknown`` record stops occupying its session.

    The deadline is set once, on the first entry into ``unknown``. Later polls,
    retries and restarts never extend it, so a lost Gateway receipt cannot keep a
    task alive forever.
    """
    if record.unknown_deadline is None:
        record.unknown_deadline = (now or utc_now()) + timedelta(seconds=timeout_s)


def _stale_dispatch_unknown(task: AgentTaskRecord) -> bool:
    """True when the task is locally abandonable without touching remote facts.

    No task-owned Action/Session is live (``pending``/``accepted``/``running``),
    and the task has at least one unfinished dispatch that PAOS can never resolve
    locally:

    * an ``unknown`` record with no ``invocation_id``; or
    * a ``failed`` record that was rejected before dispatch; or
    * a ``failed`` record whose recovery deadline expired without a confirmed
      outcome.

    Unknown records that already have a Gateway invocation identity stay
    recoverable and are not a supersede candidate. An empty task is not
    abandonable. The task still blocks its own session slot until the caller
    cancels it or starts a new task; the latter closes the stale task locally as
    failed without touching the execution records.
    """
    owned = [
        item
        for item in task.execution_records
        if item.semantics == "action"
        or (item.semantics == "session" and item.ownership == "task")
    ]
    if not owned:
        return False
    if any(item.status in {"pending", "accepted", "running"} for item in owned):
        return False
    unknown = [item for item in owned if item.status == "unknown"]
    rejected = [
        item
        for item in owned
        if item.status == "failed"
        and (item.rejected_before_dispatch or item.recovery_expired)
    ]
    if not unknown and not rejected:
        return False
    return all(item.invocation_id is None for item in unknown)


def _has_nonterminal_owned_execution(task: AgentTaskRecord) -> bool:
    return any(
        (
            item.semantics == "action"
            or (item.semantics == "session" and item.ownership == "task")
        )
        and not item.terminal
        for item in task.execution_records
    )


def _retryable_poll_error(exc: BaseException) -> bool:
    if isinstance(exc, AttributeError):
        return False
    if isinstance(exc, ForgeToolAPIError):
        if exc.retryable is False:
            return False
        if (
            exc.status_code is not None
            and 400 <= exc.status_code < 500
            and exc.status_code not in {408, 429}
        ):
            return False
    return True


def _has_cancel_retry(task: AgentTaskRecord) -> bool:
    """True when a prior cancel/stop request failed and can be safely repeated."""
    return any(_needs_cancel_retry(item) for item in task.execution_records)


def _needs_cancel_retry(item: ToolExecutionRecord) -> bool:
    """A stop/cancel POST is idempotent; retry when the last attempt errored."""
    return bool(
        item.cancel_requested
        and item.error is not None
        and item.status == "unknown"
        and (
            item.semantics == "action"
            or (item.semantics == "session" and item.ownership == "task")
        )
    )


def _cancel_terminal_status(task: AgentTaskRecord) -> AgentTaskStatus:
    owned = [
        item
        for item in task.execution_records
        if item.semantics == "action"
        or (item.semantics == "session" and item.ownership == "task")
    ]
    if not owned or all(
        item.status in {"cancelled", "stopped"} or item.rejected_before_dispatch
        for item in owned
    ):
        return AgentTaskStatus.CANCELLED
    return AgentTaskStatus.FAILED


def _execution_facts_succeeded(task: AgentTaskRecord) -> bool:
    executed = [item for item in task.execution_records if not item.rejected_before_dispatch]
    if not executed:
        # Every recorded call was rejected before dispatch: nothing ran, so the
        # task cannot be reported as succeeded.
        return False
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
        for item in executed
    )


def _remote_identity(exc: BaseException) -> tuple[str | None, str | None]:
    payload = getattr(exc, "payload", None)
    data = _response_data(payload) if isinstance(payload, dict) else {}
    invocation_id = data.get("invocation_id")
    attempt_id = data.get("attempt_id")
    return (
        invocation_id if isinstance(invocation_id, str) and invocation_id else None,
        attempt_id if isinstance(attempt_id, str) and attempt_id else None,
    )


# Gateway error codes that are only raised before anything is dispatched to a
# provider. An invoke that fails with one of these provably left no remote
# state, so its execution is definitively failed rather than unknown.
# The gateway attaches `error.code` to Action admission rejections from 1.1.2
# onward; older releases return an unstructured 503 and are therefore still
# recorded as unknown, which is the safe reading.
# FORGE_TOOL_INVOKE_TIMEOUT is deliberately absent: the gateway also emits it
# after dispatch when the synchronous caller response times out, where the
# provider may still be executing and the outcome is genuinely unknown.
# FORGE_TOOL_REQUEST_VALIDATION_REJECTED is emitted only while validating the
# caller's request, before endpoint lookup or admission. A bare 400 without this
# code (older Gateway) stays unknown.
_PRE_DISPATCH_REJECTION_CODES = frozenset(
    (
        "FORGE_TOOL_GATEWAY_BUSY",
        "FORGE_TOOL_ACTION_ADMISSION_REJECTED",
        "FORGE_TOOL_ENDPOINT_UNKNOWN",
        "FORGE_TOOL_OPERATION_NOT_FOUND",
        "FORGE_TOOL_ENDPOINT_UNAVAILABLE",
        "FORGE_TOOL_REQUEST_VALIDATION_REJECTED",
    )
)


def _rejected_before_dispatch(exc: Exception) -> bool:
    """True when the gateway rejected the invoke without dispatching it."""
    error_code = getattr(exc, "error_code", None)
    invocation_id, attempt_id = _remote_identity(exc)
    if invocation_id is not None or attempt_id is not None:
        return False
    if getattr(exc, "dispatched", None) is False:
        # The transport failed while connecting or acquiring a pool connection,
        # before any request bytes could reach the Gateway.
        return True
    if error_code == "FORGE_TOOL_REQUEST_VALIDATION_REJECTED":
        # This code is the Gateway's typed pre-dispatch validation rejection and
        # is emitted as HTTP 400. Keep the status check explicit so the same code
        # on another status is not accepted as proof.
        return getattr(exc, "status_code", None) == 400
    return error_code in _PRE_DISPATCH_REJECTION_CODES


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
