"""Durable agent-side intentions and decision evidence, separate from execution facts."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from PhyAgentOS.skill_runtime.locking import SkillOperationLock

from .contracts import canonical_json, digest


class RunnerAlreadyOwnedError(RuntimeError):
    """A live process still owns this runner; there is no hot takeover."""


class RunnerStore:
    """SQLite journal. Unknown observations are retained as append-only events.

    This database is not the executor's receipt journal. Local intents never prove
    remote delivery or physical execution. Keep it with the executor journal for
    the lifetime of a run; restoring either database requires reconciliation.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, self.SCHEMA_VERSION):
            raise ValueError(f"Unsupported interaction store version: {version}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS interaction_runs (
                run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                revision_id TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decision_steps (
                step_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES interaction_runs(run_id),
                snapshot_key TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS step_snapshot ON decision_steps(run_id, snapshot_key);
            CREATE TABLE IF NOT EXISTS model_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                step_id TEXT NOT NULL REFERENCES decision_steps(step_id), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS submission_attempts (
                submission_id TEXT PRIMARY KEY,
                step_id TEXT NOT NULL REFERENCES decision_steps(step_id),
                run_id TEXT NOT NULL REFERENCES interaction_runs(run_id), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS interaction_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, kind TEXT NOT NULL, created_at REAL NOT NULL, data TEXT NOT NULL);
            PRAGMA user_version=1;
        """)
        self._owner_file = None
        self._owner_token = None

    def acquire_owner(self, owner_token: object | None = None) -> None:
        if self._owner_file is not None:
            if self._owner_token is not owner_token:
                raise RunnerAlreadyOwnedError("RunnerStore is already owned by another Runner")
            return
        owner = open(str(self.path.resolve()) + ".owner.lock", "a+b")
        try:
            if owner.tell() == 0:
                owner.write(b"\0")
                owner.flush()
            owner.seek(0)
            SkillOperationLock._lock(owner)
        except OSError as exc:
            owner.close()
            raise RunnerAlreadyOwnedError(
                "Runner is owned by a live process; hot takeover is disabled"
            ) from exc
        self._owner_file = owner
        self._owner_token = owner_token

    def owns(self, owner_token: object) -> bool:
        return self._owner_file is not None and self._owner_token is owner_token

    def release_owner(self, owner_token: object | None = None) -> None:
        if self._owner_file is not None:
            if owner_token is not None and self._owner_token is not owner_token:
                raise RunnerAlreadyOwnedError("Cannot release another Runner's ownership")
            SkillOperationLock._unlock(self._owner_file)
            self._owner_file.close()
            self._owner_file = None
            self._owner_token = None

    def close(self) -> None:
        self.release_owner()
        self.db.close()

    _json = staticmethod(canonical_json)

    @contextmanager
    def transaction(self):
        """Commit a fact and its event together, reusing an outer transaction."""
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def event(self, run_id: str, kind: str, data: dict) -> None:
        self.db.execute(
            "INSERT INTO interaction_events(run_id,kind,created_at,data) VALUES(?,?,?,?)",
            (run_id, kind, time.time(), self._json(data)),
        )

    def create_run(self, arguments: dict, goal: Any, proposal_schema: dict) -> dict:
        run_id = arguments["interaction_run_id"]
        existing = self.get_run(run_id)
        if existing:
            if (
                existing["start_arguments"] != arguments
                or existing["goal"] != goal
                or existing["proposal_schema_digest"] != digest(proposal_schema)
            ):
                raise ValueError("Frozen run binding, goal, schema or start payload changed")
            return existing
        data = dict(
            start_arguments=arguments,
            goal=goal,
            proposal_schema_digest=digest(proposal_schema),
            start_payload_digest=digest(arguments),
            interaction_run_id=run_id,
            task_id=arguments["task_id"],
            revision_id=arguments["revision_id"],
            state="start_intent",
            created_at=time.time(),
            session_invocation_id=None,
            cancelled=False,
            recovery_required=False,
            gateway_uncertain=False,
            model_calls=0,
            reported_tokens=0,
            charged_tokens=0,
            last_snapshot=None,
            stop_requested=False,
            finalization=None,
        )
        with self.transaction():
            active = self.db.execute(
                "SELECT data FROM interaction_runs WHERE task_id=?", (arguments["task_id"],)
            ).fetchall()
            if any(json.loads(row[0])["state"] not in ("finished", "cancelled") for row in active):
                raise ValueError("Task already has a live or unresolved interaction run")
            self.db.execute(
                "INSERT INTO interaction_runs VALUES(?,?,?,?)",
                (run_id, arguments["task_id"], arguments["revision_id"], self._json(data)),
            )
            self.event(
                run_id,
                "start_intent",
                {"arguments": arguments, "digest": data["start_payload_digest"]},
            )
        return data

    def get_run(self, run_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT data FROM interaction_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def update_run(self, run_id: str, **changes: Any) -> dict:
        data = self.get_run(run_id)
        if data is None:
            raise KeyError(run_id)
        frozen = {
            "start_arguments",
            "start_payload_digest",
            "task_id",
            "revision_id",
            "interaction_run_id",
            "goal",
            "proposal_schema_digest",
            "created_at",
        }
        if frozen.intersection(changes):
            raise ValueError("Frozen run fields cannot be replaced")
        if (
            data["session_invocation_id"]
            and changes.get("session_invocation_id", data["session_invocation_id"])
            != data["session_invocation_id"]
        ):
            raise ValueError("Original Session identity cannot be replaced")
        data.update(changes)
        self.db.execute(
            "UPDATE interaction_runs SET data=? WHERE run_id=?", (self._json(data), run_id)
        )
        return data

    def create_step(self, run_id: str, step_id: str, snapshot_key: str, snapshot: dict) -> dict:
        run = self.get_run(run_id)
        observation = snapshot["observation"]
        data = dict(
            step_id=step_id,
            interaction_run_id=run_id,
            task_id=run["task_id"],
            revision_id=run["revision_id"],
            session_invocation_id=run["session_invocation_id"],
            observation_ref=observation["ref"],
            observation_version=snapshot["control_version"],
            world_epoch=observation["world_epoch"],
            snapshot_key=snapshot_key,
            status="proposed",
            created_at=time.time(),
            finished_at=None,
            execution_refs=[],
            result_ref=None,
            local_verdict=None,
            submission_id=None,
            proposal_ref=None,
            budget_usage={},
        )
        with self.transaction():
            self.db.execute(
                "INSERT INTO decision_steps VALUES(?,?,?,?)",
                (step_id, run_id, snapshot_key, self._json(data)),
            )
            self.event(
                run_id, "decision_proposed", {"step_id": step_id, "snapshot_key": snapshot_key}
            )
        return data

    def get_step(self, step_id: str) -> dict:
        row = self.db.execute(
            "SELECT data FROM decision_steps WHERE step_id=?", (step_id,)
        ).fetchone()
        if not row:
            raise KeyError(step_id)
        return json.loads(row[0])

    def update_step(self, step_id: str, **changes: Any) -> dict:
        data = self.get_step(step_id)
        frozen = {
            "step_id",
            "interaction_run_id",
            "task_id",
            "revision_id",
            "session_invocation_id",
            "observation_ref",
            "observation_version",
            "world_epoch",
            "snapshot_key",
            "created_at",
        }
        if frozen.intersection(changes):
            raise ValueError("Decision step identity and observation cannot be replaced")
        previous_status = data["status"]
        data.update(changes)
        with self.transaction():
            self.db.execute(
                "UPDATE decision_steps SET data=? WHERE step_id=?", (self._json(data), step_id)
            )
            self.event(
                data["interaction_run_id"],
                "step_reconciliation"
                if previous_status in {"unknown", "reconciling"}
                else "step_update",
                {"step_id": step_id, "previous_status": previous_status, **changes},
            )
        return data

    def steps(self, run_id: str) -> list[dict]:
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT data FROM decision_steps WHERE run_id=? ORDER BY rowid", (run_id,)
            )
        ]

    def step_for_snapshot(self, run_id: str, key: str) -> dict | None:
        row = self.db.execute(
            "SELECT data FROM decision_steps WHERE run_id=? AND snapshot_key=? ORDER BY rowid DESC LIMIT 1",
            (run_id, key),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def begin_model_attempt(self, step_id: str, token_reservation: int) -> int:
        data = {
            "started_at": time.time(),
            "status": "running",
            "token_reservation": token_reservation,
            "usage": None,
        }
        with self.transaction():
            step = self.get_step(step_id)
            run_id = step["interaction_run_id"]
            run = self.get_run(run_id)
            row = self.db.execute(
                "INSERT INTO model_attempts(step_id,data) VALUES(?,?)", (step_id, self._json(data))
            )
            self.update_run(
                run_id,
                model_calls=run["model_calls"] + 1,
                charged_tokens=run["charged_tokens"] + token_reservation,
            )
            self._update_model_budget(step_id)
            return row.lastrowid

    def finish_model_attempt(
        self, attempt_id: int, status: str, usage: dict | None, error: str | None = None
    ) -> None:
        row = self.db.execute(
            "SELECT step_id,data FROM model_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        data = json.loads(row[1])
        if data["status"] != "running":
            raise ValueError("Model attempt already accounted")
        allowed_usage = {
            "total_tokens",
            "input_tokens",
            "output_tokens",
            "prompt_tokens",
            "completion_tokens",
        }
        if (
            not isinstance(usage, dict)
            or not set(usage).issubset(allowed_usage)
            or any(type(value) is not int or value < 0 for value in usage.values())
        ):
            usage = None
        tokens = usage.get("total_tokens") if usage else None
        if type(tokens) is not int or tokens < 0:
            tokens = None
            usage = None
        data.update(status=status, finished_at=time.time(), usage=usage, error=error)
        run_id = self.get_step(row[0])["interaction_run_id"]
        with self.transaction():
            run = self.get_run(run_id)
            if tokens is not None:
                self.update_run(
                    run_id,
                    reported_tokens=run["reported_tokens"] + tokens,
                    charged_tokens=run["charged_tokens"] - data["token_reservation"] + tokens,
                )
            self.db.execute(
                "UPDATE model_attempts SET data=? WHERE attempt_id=?",
                (self._json(data), attempt_id),
            )
            self._update_model_budget(row[0])
            self.event(
                run_id, "model_attempt", {"attempt_id": attempt_id, "step_id": row[0], **data}
            )

    def model_attempts(self, step_id: str) -> list[dict]:
        return [
            {"attempt_id": r[0], **json.loads(r[1])}
            for r in self.db.execute(
                "SELECT attempt_id,data FROM model_attempts WHERE step_id=? ORDER BY attempt_id",
                (step_id,),
            )
        ]

    def _update_model_budget(self, step_id: str) -> None:
        attempts = self.model_attempts(step_id)
        previous = self.get_step(step_id)["budget_usage"]
        budget = {
            **previous,
            "model_calls": len(attempts),
            "reported_tokens": sum((a["usage"] or {}).get("total_tokens", 0) for a in attempts),
            "charged_tokens": sum(
                (a["usage"] or {}).get("total_tokens", a["token_reservation"]) for a in attempts
            ),
        }
        if budget != previous:
            self.update_step(step_id, budget_usage=budget)

    def submission_intent(self, step_id: str, payload: dict) -> dict:
        run_id = self.get_step(step_id)["interaction_run_id"]
        data = {
            "payload": payload,
            "payload_digest": digest({k: v for k, v in payload.items() if k != "runner_epoch"}),
            "status": "intent",
            "created_at": time.time(),
            "receipt": None,
            "invocation_id": None,
            "gateway_status": None,
        }
        with self.transaction():
            self.db.execute(
                "INSERT INTO submission_attempts VALUES(?,?,?,?)",
                (payload["submission_id"], step_id, run_id, self._json(data)),
            )
            self.update_step(
                step_id,
                submission_id=payload["submission_id"],
                proposal_ref=digest(payload["proposal"]),
                proposal=payload["proposal"],
            )
            self.event(
                run_id, "submission_intent", {"submission_id": payload["submission_id"], **data}
            )
        return data

    def update_submission(self, submission_id: str, **changes: Any) -> dict:
        row = self.db.execute(
            "SELECT run_id,data FROM submission_attempts WHERE submission_id=?", (submission_id,)
        ).fetchone()
        if not row:
            raise KeyError(submission_id)
        if {"payload", "payload_digest"}.intersection(changes):
            raise ValueError("Submission intent is immutable")
        data = json.loads(row[1])
        if (
            data["invocation_id"]
            and changes.get("invocation_id", data["invocation_id"]) != data["invocation_id"]
        ):
            raise ValueError("Submission transport identity cannot be replaced")
        if (
            data["receipt"] is not None
            and changes.get("receipt", data["receipt"]) != data["receipt"]
        ):
            raise ValueError("Submission receipt cannot be replaced")
        data.update(changes)
        with self.transaction():
            self.db.execute(
                "UPDATE submission_attempts SET data=? WHERE submission_id=?",
                (self._json(data), submission_id),
            )
            self.event(row[0], "submission_update", {"submission_id": submission_id, **changes})
        return data

    def submissions(self, run_id: str) -> list[dict]:
        return [
            {"submission_id": r[0], **json.loads(r[1])}
            for r in self.db.execute(
                "SELECT submission_id,data FROM submission_attempts WHERE run_id=? ORDER BY rowid",
                (run_id,),
            )
        ]

    def export_evidence(self, run_id: str) -> dict:
        steps = self.steps(run_id)
        return {
            "schema_version": self.SCHEMA_VERSION,
            "run": self.get_run(run_id),
            "steps": steps,
            "submissions": self.submissions(run_id),
            "model_attempts": {s["step_id"]: self.model_attempts(s["step_id"]) for s in steps},
            "events": [
                {"event_id": r[0], "kind": r[1], "created_at": r[2], "data": json.loads(r[3])}
                for r in self.db.execute(
                    "SELECT event_id,kind,created_at,data FROM interaction_events WHERE run_id=? ORDER BY event_id",
                    (run_id,),
                )
            ],
        }

    def bindings(self) -> list[dict]:
        from .binding import InteractionBinding

        self.db.execute(
            "CREATE TABLE IF NOT EXISTS interaction_bindings (task_id TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        return [
            InteractionBinding.model_validate_json(row[0]).model_dump()
            for row in self.db.execute("SELECT data FROM interaction_bindings")
        ]

    def bind(self, task_id: str, value: dict) -> None:
        from .binding import InteractionBinding

        value = InteractionBinding.model_validate(value).model_dump()
        self.bindings()
        old = self.binding(task_id)
        if old is not None and old != value:
            raise ValueError("interaction binding is immutable")
        self.db.execute(
            "INSERT OR IGNORE INTO interaction_bindings VALUES (?, ?)", (task_id, self._json(value))
        )

    def binding(self, task_id: str) -> dict | None:
        return next((b for b in self.bindings() if b["task_id"] == task_id), None)

    def runs(self, task_id: str | None = None) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT data FROM interaction_runs ORDER BY rowid")
            if task_id is None or json.loads(row[0])["task_id"] == task_id
        ]
