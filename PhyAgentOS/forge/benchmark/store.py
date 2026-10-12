"""Versioned benchmark journal, separate from the AgentTask database.

Every admission is persisted before dispatch. No local state implies delivery.
Owner lock covers the entire supervisor, never one of its child coroutines.
"""

import json
import sqlite3
import time
from pathlib import Path

from PhyAgentOS.forge.interaction.contracts import canonical_json, digest
from PhyAgentOS.forge.interaction.runner_store import RunnerStore


class BenchmarkStore:
    acquire_owner = RunnerStore.acquire_owner
    release_owner = RunnerStore.release_owner
    owns = RunnerStore.owns
    transaction = RunnerStore.transaction
    close = RunnerStore.close

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("unsupported benchmark store version")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL, id TEXT NOT NULL, root TEXT NOT NULL,
                data TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(kind,id));
            CREATE INDEX IF NOT EXISTS by_root ON objects(root,kind);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, root TEXT NOT NULL,
                kind TEXT NOT NULL, created REAL NOT NULL, data TEXT NOT NULL);
            PRAGMA user_version=1;
        """)
        self._owner_file = self._owner_token = None

    def put(self, kind, key, root, value, *, create=False):
        if not self.path.exists():
            raise RuntimeError("benchmark journal disappeared")
        with self.transaction():
            self.db.execute(
                ("INSERT" if create else "INSERT OR REPLACE") + " INTO objects VALUES(?,?,?,?,?)",
                (kind, key, root, canonical_json(value), digest(value)),
            )
            self.event(root, kind, {"id": key, "digest": digest(value)})
        return value

    def get(self, kind, key):
        row = self.db.execute("SELECT * FROM objects WHERE kind=? AND id=?", (kind, key)).fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row):
        value = json.loads(row["data"])
        if digest(value) != row["sha256"]:
            raise RuntimeError("benchmark journal integrity mismatch")
        return value

    def list(self, kind, root=None):
        rows = self.db.execute(
            "SELECT * FROM objects WHERE kind=?" + (" AND root=?" if root else "") + " ORDER BY id",
            (kind, root) if root else (kind,),
        )
        return [self._decode(row) for row in rows]

    def update(self, kind, key, **changes):
        with self.transaction():
            row = self.db.execute(
                "SELECT root FROM objects WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
            value = self.get(kind, key)
            if value is None:
                raise KeyError(key)
            value.update(changes)
            return self.put(kind, key, row["root"], value)

    def event(self, root, kind, data):
        self.db.execute(
            "INSERT INTO events(root,kind,created,data) VALUES(?,?,?,?)",
            (root, kind, time.time(), canonical_json(data)),
        )

    def binding(self, task_id):
        return self.get("binding", task_id)

    def bind(self, task_id, value):
        self.put("binding", task_id, task_id, value, create=True)

    def unresolved(self):
        return [run for run in self.list("run") if not run["settled"]]

    def children(self, root, *, offset=0, limit=100):
        if not 0 <= offset or not 1 <= limit <= 100:
            raise ValueError("invalid pagination")
        return self.list("subtask", root)[offset : offset + limit]
