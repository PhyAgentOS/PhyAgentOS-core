"""Crash-safe SQLite store for Agent experience and evolution jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from PhyAgentOS.agent_legacy.experience.contracts import (
    FailureObservation,
    LessonCluster,
    ScopedLesson,
    SkillCandidate,
    TaskEpisode,
    utc_now,
)


class ExperienceStore:
    def __init__(self, workspace: str | Path) -> None:
        root = Path(workspace).expanduser().resolve() / ".paos" / "evolution"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "experience.sqlite3"
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
                CREATE TABLE IF NOT EXISTS task_bindings (
                    root_task_id TEXT PRIMARY KEY,
                    binding_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS episodes (
                    episode_id TEXT PRIMARY KEY,
                    root_task_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evolution_jobs (
                    root_task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scoped_lessons (
                    lesson_id TEXT PRIMARY KEY,
                    skill_name TEXT,
                    status TEXT NOT NULL,
                    workflow_key TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS lessons_skill_status_idx
                    ON scoped_lessons(skill_name, status, updated_at);
                CREATE TABLE IF NOT EXISTS failure_observations (
                    observation_id TEXT PRIMARY KEY,
                    root_task_id TEXT NOT NULL,
                    cluster_id TEXT NOT NULL,
                    skill_name TEXT,
                    workflow_key TEXT NOT NULL,
                    pattern_key TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS observations_cluster_idx
                    ON failure_observations(cluster_id, created_at);
                CREATE TABLE IF NOT EXISTS lesson_clusters (
                    cluster_id TEXT PRIMARY KEY,
                    skill_name TEXT,
                    workflow_key TEXT NOT NULL,
                    pattern_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS lesson_clusters_scope_idx
                    ON lesson_clusters(skill_name, workflow_key, status, updated_at);
                CREATE TABLE IF NOT EXISTS lesson_cluster_support (
                    cluster_id TEXT NOT NULL,
                    root_task_id TEXT NOT NULL,
                    observation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (cluster_id, root_task_id)
                );
                CREATE TABLE IF NOT EXISTS lesson_cluster_jobs (
                    cluster_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS skill_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    skill_name TEXT NOT NULL,
                    workflow_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS candidates_status_idx
                    ON skill_candidates(status, updated_at);
                CREATE TABLE IF NOT EXISTS evolution_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evolution_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "UPDATE evolution_jobs SET status = 'pending', "
                "last_error = 'interrupted by process restart', updated_at = ? "
                "WHERE status = 'running'",
                (utc_now().isoformat(),),
            )
            connection.execute(
                "UPDATE lesson_cluster_jobs SET status = 'pending', "
                "last_error = 'interrupted by process restart', updated_at = ? "
                "WHERE status = 'running'",
                (utc_now().isoformat(),),
            )
            connection.commit()

    def save_binding(self, root_task_id: str, payload: dict[str, Any]) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO task_bindings "
                "(root_task_id, binding_json, created_at) VALUES (?, ?, ?)",
                (root_task_id, json.dumps(payload, ensure_ascii=False), now),
            )
            self._event(connection, "task_bound", root_task_id, {})
            connection.commit()

    def get_binding(self, root_task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT binding_json FROM task_bindings WHERE root_task_id = ?",
                (root_task_id,),
            ).fetchone()
        return json.loads(row["binding_json"]) if row else None

    def create_episode(self, episode: TaskEpisode, *, enqueue: bool) -> bool:
        now = utc_now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM episodes WHERE root_task_id = ?", (episode.root_task_id,)
            ).fetchone()
            if existing:
                connection.rollback()
                return False
            connection.execute(
                "INSERT INTO episodes "
                "(episode_id, root_task_id, status, record_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    episode.episode_id,
                    episode.root_task_id,
                    episode.processing_status,
                    episode.model_dump_json(),
                    episode.created_at.isoformat(),
                    now,
                ),
            )
            if enqueue:
                connection.execute(
                    "INSERT INTO evolution_jobs "
                    "(root_task_id, status, attempts, updated_at) VALUES (?, 'pending', 0, ?)",
                    (episode.root_task_id, now),
                )
            self._event(
                connection,
                "episode_created",
                episode.episode_id,
                {"enqueued": enqueue, "root_task_id": episode.root_task_id},
            )
            connection.commit()
        return True

    def get_episode_by_root(self, root_task_id: str) -> TaskEpisode:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM episodes WHERE root_task_id = ?", (root_task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"experience episode not found: {root_task_id}")
        return TaskEpisode.model_validate_json(row["record_json"])

    def get_episode(self, episode_id: str) -> TaskEpisode | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
        return TaskEpisode.model_validate_json(row["record_json"]) if row else None

    def update_episode(self, episode: TaskEpisode) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE episodes SET status = ?, record_json = ?, updated_at = ? "
                "WHERE episode_id = ?",
                (
                    episode.processing_status,
                    episode.model_dump_json(),
                    utc_now().isoformat(),
                    episode.episode_id,
                ),
            )
            connection.commit()

    def pending_jobs(self) -> list[str]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT root_task_id FROM evolution_jobs WHERE status = 'pending' "
                "ORDER BY updated_at"
            ).fetchall()
        return [row["root_task_id"] for row in rows]

    def start_job(self, root_task_id: str) -> bool:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM evolution_jobs WHERE root_task_id = ?",
                (root_task_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                connection.rollback()
                return False
            connection.execute(
                "UPDATE evolution_jobs SET status = 'running', attempts = attempts + 1, "
                "updated_at = ? WHERE root_task_id = ?",
                (utc_now().isoformat(), root_task_id),
            )
            connection.commit()
        return True

    def finish_job(self, root_task_id: str) -> None:
        self._set_job_status(root_task_id, "completed", None)

    def fail_job(self, root_task_id: str, error: str, *, retry: bool) -> None:
        self._set_job_status(root_task_id, "pending" if retry else "failed", error[:1000])

    def defer_job(self, root_task_id: str) -> None:
        self._set_job_status(root_task_id, "pending", "evolution call budget exhausted")

    def _set_job_status(self, root_task_id: str, status: str, error: str | None) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE evolution_jobs SET status = ?, last_error = ?, updated_at = ? "
                "WHERE root_task_id = ?",
                (status, error, utc_now().isoformat(), root_task_id),
            )
            connection.commit()

    def job_attempts(self, root_task_id: str) -> int:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT attempts FROM evolution_jobs WHERE root_task_id = ?", (root_task_id,)
            ).fetchone()
        return int(row["attempts"]) if row else 0

    def upsert_lesson(self, lesson: ScopedLesson) -> ScopedLesson:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM scoped_lessons WHERE lesson_id = ?",
                (lesson.lesson_id,),
            ).fetchone()
            event = "lesson_activated"
            if row:
                current = ScopedLesson.model_validate_json(row["record_json"])
                current.source_episode_ids = list(
                    dict.fromkeys(current.source_episode_ids + lesson.source_episode_ids)
                )
                current.supporting_episode_ids = list(
                    dict.fromkeys(
                        current.supporting_episode_ids
                        + lesson.supporting_episode_ids
                    )
                )
                current.supersedes_lesson_ids = list(
                    dict.fromkeys(
                        current.supersedes_lesson_ids
                        + lesson.supersedes_lesson_ids
                    )
                )
                current.observation_count = len(
                    current.supporting_episode_ids or current.source_episode_ids
                )
                if lesson.cluster_id and current.cluster_id == lesson.cluster_id:
                    current.applies_when = lesson.applies_when
                    current.does_not_apply_when = lesson.does_not_apply_when
                    current.failure_mode = lesson.failure_mode
                    current.recommendation = lesson.recommendation
                    current.severity = lesson.severity
                current.updated_at = utc_now()
                if current.status in {"inactive", "superseded", "retired"}:
                    current.status = lesson.status
                    current.superseded_by_lesson_id = lesson.superseded_by_lesson_id
                lesson = current
                event = "lesson_observed"
            connection.execute(
                "INSERT INTO scoped_lessons "
                "(lesson_id, skill_name, status, workflow_key, record_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(lesson_id) DO UPDATE SET skill_name=excluded.skill_name, "
                "status=excluded.status, workflow_key=excluded.workflow_key, "
                "record_json=excluded.record_json, updated_at=excluded.updated_at",
                (
                    lesson.lesson_id,
                    lesson.skill_name,
                    lesson.status,
                    lesson.workflow_key,
                    lesson.model_dump_json(),
                    lesson.updated_at.isoformat(),
                ),
            )
            self._event(connection, event, lesson.lesson_id, {"skill": lesson.skill_name})
            connection.commit()
        return lesson

    def get_lesson(self, lesson_id: str) -> ScopedLesson | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM scoped_lessons WHERE lesson_id = ?", (lesson_id,)
            ).fetchone()
        return ScopedLesson.model_validate_json(row["record_json"]) if row else None

    def update_lesson(
        self, lesson_id: str, mutate: Callable[[ScopedLesson], None], *, event_type: str
    ) -> ScopedLesson | None:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM scoped_lessons WHERE lesson_id = ?", (lesson_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            lesson = ScopedLesson.model_validate_json(row["record_json"])
            mutate(lesson)
            lesson.updated_at = utc_now()
            connection.execute(
                "UPDATE scoped_lessons SET skill_name = ?, status = ?, workflow_key = ?, "
                "record_json = ?, updated_at = ? "
                "WHERE lesson_id = ?",
                (
                    lesson.skill_name,
                    lesson.status,
                    lesson.workflow_key,
                    lesson.model_dump_json(),
                    lesson.updated_at.isoformat(),
                    lesson_id,
                ),
            )
            self._event(connection, event_type, lesson_id, {})
            connection.commit()
        return lesson

    def list_lessons(
        self, *, skill_name: str | None = None, status: str | None = None
    ) -> list[ScopedLesson]:
        clauses, params = [], []
        if skill_name is not None:
            clauses.append("skill_name = ?")
            params.append(skill_name)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT record_json FROM scoped_lessons" + where + " ORDER BY updated_at DESC",
                tuple(params),
            ).fetchall()
        return [ScopedLesson.model_validate_json(row["record_json"]) for row in rows]

    def get_cluster(self, cluster_id: str) -> LessonCluster | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM lesson_clusters WHERE cluster_id = ?",
                (cluster_id,),
            ).fetchone()
        return LessonCluster.model_validate_json(row["record_json"]) if row else None

    def list_clusters(
        self, *, skill_name: str | None = None, status: str | None = None
    ) -> list[LessonCluster]:
        clauses: list[str] = []
        params: list[str] = []
        if skill_name is not None:
            clauses.append("skill_name = ?")
            params.append(skill_name)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT record_json FROM lesson_clusters"
                + where
                + " ORDER BY updated_at DESC",
                tuple(params),
            ).fetchall()
        return [LessonCluster.model_validate_json(row["record_json"]) for row in rows]

    def upsert_cluster(self, cluster: LessonCluster) -> LessonCluster:
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM lesson_clusters WHERE cluster_id = ?",
                (cluster.cluster_id,),
            ).fetchone()
            if row:
                cluster = LessonCluster.model_validate_json(row["record_json"])
            else:
                cluster.updated_at = now
                connection.execute(
                    "INSERT INTO lesson_clusters "
                    "(cluster_id, skill_name, workflow_key, pattern_key, status, "
                    "record_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        cluster.cluster_id,
                        cluster.skill_name,
                        cluster.workflow_key,
                        cluster.pattern_key,
                        cluster.status,
                        cluster.model_dump_json(),
                        now.isoformat(),
                    ),
                )
                self._event(connection, "lesson_cluster_created", cluster.cluster_id, {})
            connection.commit()
        return cluster

    def add_observation(
        self, observation: FailureObservation, cluster: LessonCluster
    ) -> tuple[LessonCluster, bool]:
        """Persist one normalized observation and count its root at most once."""
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM lesson_clusters WHERE cluster_id = ?",
                (cluster.cluster_id,),
            ).fetchone()
            if row:
                cluster = LessonCluster.model_validate_json(row["record_json"])
            else:
                connection.execute(
                    "INSERT INTO lesson_clusters "
                    "(cluster_id, skill_name, workflow_key, pattern_key, status, "
                    "record_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        cluster.cluster_id,
                        cluster.skill_name,
                        cluster.workflow_key,
                        cluster.pattern_key,
                        cluster.status,
                        cluster.model_dump_json(),
                        now.isoformat(),
                    ),
                )
                self._event(connection, "lesson_cluster_created", cluster.cluster_id, {})
            inserted_observation = connection.execute(
                "INSERT OR IGNORE INTO failure_observations "
                "(observation_id, root_task_id, cluster_id, skill_name, workflow_key, "
                "pattern_key, record_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation.observation_id,
                    observation.root_task_id,
                    observation.cluster_id,
                    observation.skill_name,
                    observation.workflow_key,
                    observation.pattern_key,
                    observation.model_dump_json(),
                    observation.created_at.isoformat(),
                ),
            ).rowcount > 0
            inserted_support = connection.execute(
                "INSERT OR IGNORE INTO lesson_cluster_support "
                "(cluster_id, root_task_id, observation_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    cluster.cluster_id,
                    observation.root_task_id,
                    observation.observation_id,
                    now.isoformat(),
                ),
            ).rowcount > 0
            observations = connection.execute(
                "SELECT observation_id FROM failure_observations "
                "WHERE cluster_id = ? ORDER BY created_at",
                (cluster.cluster_id,),
            ).fetchall()
            roots = connection.execute(
                "SELECT root_task_id FROM lesson_cluster_support "
                "WHERE cluster_id = ? ORDER BY created_at",
                (cluster.cluster_id,),
            ).fetchall()
            cluster.observation_ids = [item["observation_id"] for item in observations]
            cluster.supporting_root_task_ids = [item["root_task_id"] for item in roots]
            cluster.recovery_principles = list(
                dict.fromkeys(
                    cluster.recovery_principles + [observation.recovery_principle]
                )
            )
            if inserted_support and cluster.status == "blocked":
                cluster.status = "collecting"
                cluster.draft = None
                cluster.validation = None
                cluster.validation_errors = []
            cluster.updated_at = now
            connection.execute(
                "UPDATE lesson_clusters SET status = ?, record_json = ?, updated_at = ? "
                "WHERE cluster_id = ?",
                (
                    cluster.status,
                    cluster.model_dump_json(),
                    now.isoformat(),
                    cluster.cluster_id,
                ),
            )
            if inserted_observation:
                self._event(
                    connection,
                    "observation_clustered",
                    observation.observation_id,
                    {"cluster_id": cluster.cluster_id},
                )
            if inserted_support:
                self._event(
                    connection,
                    "lesson_cluster_supported",
                    cluster.cluster_id,
                    {"support_count": len(cluster.supporting_root_task_ids)},
                )
            connection.commit()
        return cluster, inserted_support

    def list_observations(self, cluster_id: str) -> list[FailureObservation]:
        """Return one normalized observation for each independently counted root."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT observations.record_json FROM lesson_cluster_support AS support "
                "JOIN failure_observations AS observations "
                "ON observations.observation_id = support.observation_id "
                "WHERE support.cluster_id = ? ORDER BY support.created_at",
                (cluster_id,),
            ).fetchall()
        return [FailureObservation.model_validate_json(row["record_json"]) for row in rows]

    def update_cluster(self, cluster: LessonCluster, *, event_type: str) -> None:
        cluster.updated_at = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE lesson_clusters SET skill_name = ?, workflow_key = ?, "
                "pattern_key = ?, status = ?, record_json = ?, updated_at = ? "
                "WHERE cluster_id = ?",
                (
                    cluster.skill_name,
                    cluster.workflow_key,
                    cluster.pattern_key,
                    cluster.status,
                    cluster.model_dump_json(),
                    cluster.updated_at.isoformat(),
                    cluster.cluster_id,
                ),
            )
            self._event(connection, event_type, cluster.cluster_id, {})
            connection.commit()

    def enqueue_cluster_job(self, cluster_id: str) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO lesson_cluster_jobs "
                "(cluster_id, status, attempts, updated_at) VALUES (?, 'pending', 0, ?) "
                "ON CONFLICT(cluster_id) DO UPDATE SET status = "
                "CASE WHEN status = 'running' THEN status ELSE 'pending' END, "
                "updated_at = excluded.updated_at",
                (cluster_id, now),
            )
            connection.commit()

    def pending_cluster_jobs(self) -> list[str]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT cluster_id FROM lesson_cluster_jobs WHERE status = 'pending' "
                "ORDER BY updated_at"
            ).fetchall()
        return [row["cluster_id"] for row in rows]

    def start_cluster_job(self, cluster_id: str) -> bool:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE lesson_cluster_jobs SET status = 'running', "
                "attempts = attempts + 1, updated_at = ? "
                "WHERE cluster_id = ? AND status = 'pending'",
                (utc_now().isoformat(), cluster_id),
            ).rowcount
            connection.commit()
        return bool(changed)

    def finish_cluster_job(self, cluster_id: str) -> None:
        self._set_cluster_job_status(cluster_id, "completed", None)

    def fail_cluster_job(self, cluster_id: str, error: str, *, retry: bool) -> None:
        self._set_cluster_job_status(
            cluster_id, "pending" if retry else "failed", error[:200]
        )

    def defer_cluster_job(self, cluster_id: str) -> None:
        self._set_cluster_job_status(cluster_id, "pending", "evolution budget exhausted")

    def cluster_job_attempts(self, cluster_id: str) -> int:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT attempts FROM lesson_cluster_jobs WHERE cluster_id = ?",
                (cluster_id,),
            ).fetchone()
        return int(row["attempts"]) if row else 0

    def _set_cluster_job_status(
        self, cluster_id: str, status: str, error: str | None
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE lesson_cluster_jobs SET status = ?, last_error = ?, updated_at = ? "
                "WHERE cluster_id = ?",
                (status, error, utc_now().isoformat(), cluster_id),
            )
            connection.commit()

    def upsert_candidate(self, candidate: SkillCandidate) -> SkillCandidate:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM skill_candidates WHERE candidate_id = ?",
                (candidate.candidate_id,),
            ).fetchone()
            event = "candidate_created"
            if row:
                current = SkillCandidate.model_validate_json(row["record_json"])
                current.supporting_episode_ids = list(
                    dict.fromkeys(
                        current.supporting_episode_ids + candidate.supporting_episode_ids
                    )
                )
                current.proposal = candidate.proposal
                current.updated_at = utc_now()
                if current.status not in {"promoted", "rejected"}:
                    current.status = candidate.status
                    current.blocked_by_lesson_ids = candidate.blocked_by_lesson_ids
                    current.validation_errors = candidate.validation_errors
                candidate = current
                event = "candidate_supported"
            connection.execute(
                "INSERT INTO skill_candidates "
                "(candidate_id, skill_name, workflow_key, status, record_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(candidate_id) DO UPDATE SET status=excluded.status, "
                "record_json=excluded.record_json, updated_at=excluded.updated_at",
                (
                    candidate.candidate_id,
                    candidate.proposal.skill_name,
                    candidate.proposal.workflow_key,
                    candidate.status,
                    candidate.model_dump_json(),
                    candidate.updated_at.isoformat(),
                ),
            )
            self._event(
                connection,
                event,
                candidate.candidate_id,
                {"support_count": len(candidate.supporting_episode_ids)},
            )
            connection.commit()
        return candidate

    def update_candidate(self, candidate: SkillCandidate, *, event_type: str) -> None:
        candidate.updated_at = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE skill_candidates SET status = ?, record_json = ?, updated_at = ? "
                "WHERE candidate_id = ?",
                (
                    candidate.status,
                    candidate.model_dump_json(),
                    candidate.updated_at.isoformat(),
                    candidate.candidate_id,
                ),
            )
            self._event(connection, event_type, candidate.candidate_id, {})
            connection.commit()

    def list_candidates(self, *, active_only: bool = False) -> list[SkillCandidate]:
        where = " WHERE status IN ('collecting', 'blocked')" if active_only else ""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT record_json FROM skill_candidates" + where + " ORDER BY updated_at DESC"
            ).fetchall()
        return [SkillCandidate.model_validate_json(row["record_json"]) for row in rows]

    def record_event(
        self, event_type: str, subject_id: str, payload: dict[str, Any] | None = None
    ) -> None:
        with self._lock, self._connection() as connection:
            self._event(connection, event_type, subject_id, payload or {})
            connection.commit()

    def metadata(self, key: str) -> str | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM evolution_metadata WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def set_metadata(self, key: str, value: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO evolution_metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            connection.commit()

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        event_type: str,
        subject_id: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO evolution_events "
            "(event_type, subject_id, created_at, payload_json) VALUES (?, ?, ?, ?)",
            (
                event_type,
                subject_id,
                utc_now().isoformat(),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
